"""Guard: a ``[string[]]`` script parameter must survive ``pwsh -File x.ps1 -P a,b``.

Under ``-File`` every argument is ONE literal string, so ``-Dirs a,b`` binds a single element
``"a,b"`` and a ``[string[]]`` parameter accepts it silently: a membership or count check then
passes or matches nothing. Bus TRAPS.md 2026-08-09 (first-hand, adversarialllm OPUS) and the
2026-10-04 agent-bridge entry (bus commit fa47038) record the same fail-open. This module turns
the trap into a check that hosted CI runs.

The rule: a script-level ``[string[]]`` parameter must split a comma-joined single element
(``-split ','``, ``.Split(',')`` or a ``$P = Resolve-...  $P`` helper) WHEN a tracked file shows
that script being invoked with ``-File`` (or in its own ``Usage:`` text) and a comma list for
that parameter.

A caller that hands an ARRAY to a ``-File`` child is flagged whether or not the callee splits: ``-P $arr``,
``-P @(...)`` and the quoted ArgumentList form ``'-File', 'x.ps1', '-P', $arr`` expand to separate tokens, so
only the first element binds (export-release-cuda-dogfood-kit.ps1 and invoke-ultramagnus-p3-evidence.ps1 were
this shape). Fix the caller with ``($arr -join ',')`` and split in the script. A variable counts as an array
when the caller's file declares it ``[string[]]`` / ``[array]``, assigns it ``@(...)`` / a cast / a comma list /
a ``-split`` / a ``Verb-...Array...`` helper (never a name that merely contains "array", like ``$arrayCount``),
or aliases one of those. A split clears the parameter only in one of two whitelisted forms (``is_normalised``), and
only when it splits ON A COMMA, and the credit is a whitelist of exact shapes: the ``-split`` delimiter must be ``','``
or ``','`` padded by ``\\s*`` / `` *`` on either side, with a limit that is absent or the literal 0; a ``.Split(...)``
must take exactly one comma (``','``, ``[char]','``, ``@(',')``, ``[char[]]','``) and no count, option or second
separator. A ``';'``, newline, ``', '``, empty or interpolated delimiter, a lookaround, alternation, class or
escape, or any positive limit leaves ``-P a,b`` (or ``-P a,b,c``) whole or partly joined and is flagged
(NON_COMMA_LISTS names the live scripts that split on something else on purpose). Every other shape is flagged. TEMP/DIRECT: ``$X = $P -split ','`` or ``$X = $P[0].Split(',')`` (``$X`` may be ``$P``), with
``$X`` later read as a bare token. STAGE: ``$P | ForEach-Object { $_ -split ',' }`` (or ``%``), whose block is that
one split expression and nothing else (or exactly ``if (Test-Path -LiteralPath $_) { $_ } else { <that split> }``), assigned back to ``$P``, assigned to a variable read later, or piped on
(never into ``Out-Null`` or ``> $null``; a ``$null`` or ``[void]`` target never counts). Only top-level script code
counts: a split or a read inside a ``function`` / ``filter`` definition (name, parameter list, body) is ignored. Every
comment and string literal (both quote kinds, both here-string kinds) is blanked first, so neither a split nor a read
inside a string counts. A ``[ValidateSet]`` on a top-level ``[string[]]`` parameter is never cleared by a split (it
rejects the joined element at bind time), so a comma-list caller is flagged.

NON-PROMISES (what this does NOT see):
- Regex over text, no PowerShell AST and no pwsh process. A comma list assembled at run time
  (``-join ','`` into a variable) is not seen; a variable of unknown type (``-P $x``) is not flagged;
  a caller that only appears in prose (docs/playback-attr-3-cuda.md names ``-CudaArchitectures
  sm_86,compute_86`` with no ``-File`` on the line) is not seen, so the dll-job split has no live-tree
  revert test. A flow of the split result through a function call or a property (``$x = Normalise $P``, with the
  split inside the function) is not followed, so only the ``Resolve-...`` helper shape is recognised.
- The delimiter is judged as a literal: a separator held in a variable (``-split $sep``), built by ``-f`` or ``[char]44``,
  or any ``-split`` option beyond the limit is not credited (fail closed). The delimiter is matched against the
  whitelist text, never run as a regex, so a spelling .NET reads as a comma (``'\\,'``, ``'\\x2c'``, ``'[,]'``) is
  refused too. A positive limit is refused because the caller's list length is unknown: ``-split ',',2`` leaves the
  tail of ``a,b,c`` joined.
- Only the first column-0 ``param(`` of a file is read, so a function-level parameter is out of
  scope, and so is a script that nests its real param block in a here-string.
- Pass-through parameters (``-AdditionalArgs`` and friends) cannot be fixed by splitting on a
  comma, because their values may legitimately contain commas. They are listed in KNOWN_OPEN with
  the reason and are asserted to still be violations, so the list cannot go stale.
- tools/hooks/* is out of scope (read-only for this guard).

Collected by ``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; no workflow names it.
"""

from __future__ import annotations

import functools
import re
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_PREFIXES = ("tools/hooks/",)
# This module's fixtures spell out the very shapes it flags (and name real scripts), so it is not a caller.
SELF = "tools/repo_hygiene/test_pwsh_string_array_file_params.py"
TEXT_SUFFIXES = frozenset((
    ".ps1", ".psm1", ".py", ".md", ".json", ".yml", ".yaml", ".cmd", ".bat", ".txt", ".sh", ".mjs", ".js",
))
MAX_BYTES = 2_000_000
MAX_COMMA_JOIN = 60

# (script path, parameter) -> why splitting does not apply and where the follow-up lives.
# Each entry is asserted to STILL be a violation: fix the script or its caller, then delete it.
KNOWN_OPEN: dict[tuple[str, str], str] = {
    ("tools/profiling/run-release-gui-smoke.ps1", "ExtraEnvironment"): (
        "docs/04-external-auditor-guide.md:504 documents `-ExtraEnvironment @('KEY=VALUE')` with ONE element, which "
        "expands to one token and binds correctly; the guard flags the `@(...)` literal because it cannot count "
        "elements. Observed under pwsh 7.6.6 (`pwsh -File`, `@(...)` expanded by the calling shell): with two elements the "
        "second does NOT reach $UnrecognizedArguments, because ValueFromRemainingArguments only takes tokens no positional "
        "parameter claims; it binds positionally to ExePath and the unrecognized-arguments gate passes without throwing. "
        "A third lands on the [double] -Seconds and fails type conversion at bind time. Pass-through values may hold "
        "commas, so a comma split is not the fix; follow-up PWSH-FILE-ARRAY-PASSTHRU-1 (queued in the hub card queue): "
        "fail loud or pass base64 JSON."
    ),
    ("tools/profiling/run-release-playback-profile.ps1", "AdditionalArgs"): (
        "docs/14-performance-benchmarking.md:544-551 documents `-AdditionalArgs @('--stage-log', ..., '--raw-cache-mb', "
        "...)`, six elements. Observed under pwsh 7.6.6 (`pwsh -File`, `@(...)` expanded by the calling shell): the six "
        "tokens bind as the first to -AdditionalArgs, the second positionally to ExePath (the stage-log path), and the "
        "other four (`--raw-cache-mb 128 --cache-cpu-cores 4`) land silently in $args (no [CmdletBinding()] and no "
        "ValueFromRemainingArguments, so nothing throws at bind time); the failure of that example, if any, comes later "
        "when the script resolves ExePath. A two-element list binds the second element to ExePath the same way. Same "
        "follow-up PWSH-FILE-ARRAY-PASSTHRU-1."
    ),
}

# (script path, parameter) -> why the parameter is a list that is deliberately NOT comma-joined, so its split is
# not a comma split and the guard no longer credits it (SPLIT-DELIMITER-NOT-CHECKED-1). Not violations: no tracked
# caller hands any of them a comma list. Each entry is asserted to STILL be uncredited and unflagged, so it cannot go stale;
# if a caller ever passes `-P a,b` the guard flags it and the callee must then split on a comma.
NON_COMMA_LISTS: dict[tuple[str, str], str] = {
    ("tools/profiling/run-ultramagnus-p3-validation.ps1", "EvidenceGitStatus"): (
        "splits on a newline (`r?`n): the value is `git status --short` lines, which may hold a comma in a file name, "
        "and invoke-ultramagnus-p3-evidence.ps1 joins them with [char]10. A comma split would corrupt them."
    ),
    ("tools/repo_hygiene/attr3_publish_write_scan.ps1", "GeneratorPath"): (
        "splits on ';' (header comment: `pwsh -File` cannot pass an array, so ';'-separated lists are accepted); paths "
        "may hold a comma, and no tracked caller passes a comma list (test_playback_attr_3_cuda_behaviour.py:4672 passes "
        "';'-joined values)."
    ),
    ("tools/repo_hygiene/attr3_publish_write_scan.ps1", "TemplateFile"): "Same ';' path list as GeneratorPath.",
    ("tools/repo_hygiene/attr3_publish_write_scan.ps1", "ModulePath"): "Same ';' path list as GeneratorPath.",
}


@dataclass(frozen=True)
class Violation:
    script: str
    param: str
    caller: str
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.script} -{self.param}: {self.caller}:{self.line}: {self.detail}"


_HERE_OPEN = re.compile(r"@(['\"])[ \t]*\r?\n")


_COMMA_EXACT = "\x02"  # a blanked literal that is exactly ','
_COMMA_CLASS = "\x01"  # a blanked literal that is ',' padded by '\s*' / ' *' on one or both sides
# The r2 whitelist: the only regex spellings of "one comma and nothing else" that are credited. Native: each splits
# `-Dirs a,b,c` into three elements. `\s+` is NOT on it: `'\s+,\s+'` needs whitespace around the comma, so it leaves
# `a,b,c` whole. A lookaround, alternation, class, group, anchor, escape, quantifier on the comma or any other text
# is not credited, however it behaves on one sample.
_COMMA_DELIM = re.compile(r"(?:\\s\*| \*)?,(?:\\s\*| \*)?")


def _comma_fill(content: str) -> str:
    """What a blanked string literal keeps so a split can still be judged by its delimiter, else ``""``.

    A literal that is exactly ``,`` becomes one ``_COMMA_EXACT``; ``,`` with ``\\s*`` or `` *`` on either side
    becomes ``_COMMA_CLASS`` repeated to the same length. Anything else (``;``, a newline, ``', '``, ``''``, an
    interpolated ``"$sep"``, ``'[,;]'``, ``',(?=[a-z])'``) is blanked as before, so a split on it is never credited.
    """
    if content == ",":
        return _COMMA_EXACT
    return _COMMA_CLASS * len(content) if _COMMA_DELIM.fullmatch(content) else ""


def _blank_comments(text: str, literals: bool = False) -> str:
    """Return text with PowerShell comments replaced by spaces (newlines kept so line numbers hold).

    ``literals=True`` also blanks the inside of every string literal: single- and double-quoted strings and both
    here-string kinds (the delimiters stay). Text inside a string is never a read and never a split, even where
    PowerShell interpolates it. The one thing a single- or double-quoted literal keeps is whether it is a comma
    delimiter (``_comma_fill``), so a split can be judged by what it splits on. The length never changes, so
    offsets still line up.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    lit_out = lit_start = 0
    while i < n:
        c = text[i]
        if quote:
            if quote == '"' and c == "`" and i + 1 < n:
                nxt = text[i + 1]
                out.append(" " if literals else c)
                out.append(" " if literals and nxt not in "\r\n" else nxt)
                i += 2
                continue
            if c == quote:
                if i + 1 < n and text[i + 1] == quote:
                    out.append("  " if literals else quote * 2)
                    i += 2
                    continue
                out.append(c)
                if literals and (fill := _comma_fill(text[lit_start:i])):
                    out[lit_out:-1] = [fill]
                quote = ""
            else:
                out.append(" " if literals and c not in "\r\n" else c)
            i += 1
            continue
        here = _HERE_OPEN.match(text, i) if literals and c == "@" else None
        if here:
            q = here.group(1)
            close = text.find("\n" + q + "@", here.end() - 1)
            end = n if close < 0 else close + 3
            inner_end = n if close < 0 else max(close, here.end())
            out.append(text[i:here.end()])
            out.append("".join(ch if ch in "\r\n" else " " for ch in text[here.end():inner_end]))
            out.append(text[inner_end:end])
            i = end
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            lit_out, lit_start = len(out), i + 1
        elif c == "<" and text.startswith("<#", i):
            end = text.find("#>", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(ch if ch in "\r\n" else " " for ch in text[i:end]))
            i = end
            continue
        elif c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def script_string_array_params(text: str) -> list[tuple[str, int, int]]:
    """(name, line, param_block_end_offset) for each ``[string[]]`` parameter of the first column-0 param block."""
    clean = _blank_comments(text)
    m = re.search(r"(?im)^param\s*\(", clean)
    if not m:
        return []
    depth, i, quote = 1, m.end(), ""
    while i < len(clean) and depth:
        c = clean[i]
        if quote:
            if quote == '"' and c == "`":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    block = clean[m.end():i]
    found = []
    for pm in re.finditer(r"\[string\[\]\]\s*\$(\w+)", block, re.I):
        line = clean.count("\n", 0, m.end() + pm.start()) + 1
        found.append((pm.group(1), line, i))
    return found


def _statements(body: str) -> list[str]:
    """Split comment-blanked PowerShell into whitespace-collapsed statements.

    A statement ends at a newline or ``;`` unless it is continued: a trailing ``|``, ``,`` or backtick, a next
    line that starts with ``|``, an open ``(`` / ``[``, or an open ``{`` of a pipeline script block (a ``{`` after
    a ``|``). Plain ``{`` / ``}`` (function, if, foreach bodies) end a statement, so one statement never spans a
    whole block and an unrelated ``-split`` cannot ride along with a parameter mention.
    """
    out: list[str] = []
    cur: list[str] = []
    braces: list[bool] = []
    depth, quote, i, n = 0, "", 0, len(body)
    # Linear scan: the statement's last significant character and "has a pipe" are tracked as it is built, and
    # the next non-blank character is found once per whitespace run, so no step re-reads the text around it.
    last, piped, ahead_at = "", False, 0

    def add(piece: str) -> None:
        nonlocal last, piped
        cur.append(piece)
        if "|" in piece:
            piped = True
        if piece.strip():
            last = piece.rstrip()[-1]

    def flush() -> None:
        nonlocal last, piped
        stmt = " ".join("".join(cur).split())
        if stmt:
            out.append(stmt)
        cur.clear()
        last, piped = "", False

    while i < n:
        c = body[i]
        if quote:
            add(c)
            if quote == '"' and c == "`" and i + 1 < n:
                add(body[i + 1])
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < n and body[i + 1] == "'":
                    add("'")
                    i += 2
                    continue
                quote = ""
        elif c in ("'", '"'):
            quote = c
            add(c)
        elif c == "`" and i + 1 < n and body[i + 1] in "\r\n":
            add(" ")
            i += 2
            continue
        elif c in "([":
            depth += 1
            add(c)
        elif c in ")]":
            depth = max(0, depth - 1)
            add(c)
        elif c == "{":
            counted = piped
            braces.append(counted)
            depth += 1 if counted else 0
            add(c)
            if not counted and depth == 0:
                flush()
        elif c == "}":
            counted = braces.pop() if braces else False
            depth = max(0, depth - (1 if counted else 0))
            add(c)
            if not counted and depth == 0:
                flush()
        elif c == ";" and depth == 0:
            flush()
        elif c == "\n":
            if ahead_at <= i:
                ahead_at = i + 1
                while ahead_at < n and body[ahead_at].isspace():
                    ahead_at += 1
            if depth or last in ("|", ",") or (ahead_at < n and body[ahead_at] == "|"):
                add(" ")
            else:
                flush()
        else:
            add(c)
        i += 1
    flush()
    return out


@functools.lru_cache(maxsize=8)
def _body_statements(text: str, block_end: int) -> tuple[str, ...]:
    """Top-level statements after the param block; cached because every parameter of a script reads the same body."""
    return tuple(_statements(_blank_functions(_blank_comments(text, literals=True)[block_end:])))


# The r2 whitelist of exact shapes; everything else fails closed. Statements reach these patterns with every string
# literal blanked to spaces, except that a comma delimiter keeps a marker (``_comma_fill``). A split expression is
# ``<x> -split '<comma>'[, 0]``, ``(<x> -split '<comma>').Trim()`` or ``<x>.Split(<one comma>)[.Trim()]``, and it must
# be the WHOLE right-hand side or stage body. A positive -split limit is refused (the caller's list length is unknown,
# so `-split ',',2` on `a,b,c` leaves `b,c` joined); so is any option string (`-split ',',0,'SimpleMatch'` works
# natively but is not credited) and any `.Split` count, option or second separator (`.Split('x', ',')` throws natively).
_ITEM = r"(?:\$_\b|\$PSItem\b|\(\s*\[string\]\s*(?:\$_|\$PSItem)\s*\))"
_COMMA_LIT = r"(?:'[\x01\x02]+'|\"[\x01\x02]+\")"
_SPLIT_LIMIT = r"(?:\s*,\s*0(?![\w.]))?"
# A .Split() argument list that is exactly one comma: ``','``, ``[char]','``, ``@(',')`` or ``[char[]]`` over either.
_SPLIT_LIT = r"(?:'\x02'|\"\x02\")"
_SPLIT_CHAR = r"(?:\[char\]\s*)?" + _SPLIT_LIT
_SPLIT_ARR = r"@\(\s*" + _SPLIT_CHAR + r"\s*\)"
_SPLIT_ARGS = r"\s*(?:" + _SPLIT_CHAR + r"|" + _SPLIT_ARR + r"|\[char\[\]\]\s*(?:" + _SPLIT_LIT + r"|" + _SPLIT_ARR + r"))\s*"
_TRIM = r"(?:\.Trim\(\))?"


def _split_of(operand: str) -> str:
    by_op = operand + r"\s*-split\s*" + _COMMA_LIT + _SPLIT_LIMIT
    return r"(?:" + by_op + r"|\(\s*" + by_op + r"\s*\)" + _TRIM + r"|" + operand + r"\.Split\(" + _SPLIT_ARGS + r"\)" + _TRIM + r")"


# The one guarded variant: an existing path passes through whole, anything else is split (a path may hold a comma).
_STAGE_BODY = re.compile(
    r"\s*(?:" + _split_of(_ITEM) + r"|if\s*\(\s*Test-Path\s+-LiteralPath\s+" + _ITEM + r"\s*\)\s*\{\s*" + _ITEM
    + r"\s*\}\s*else\s*\{\s*" + _split_of(_ITEM) + r"\s*\})\s*",
    re.I,
)
_CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted string that opens at ``i`` (an unterminated one runs to the end)."""
    q, i = text[i], i + 1
    while i < len(text):
        if q == '"' and text[i] == "`":
            i += 1
        elif text[i] == q:
            return i + 1
        i += 1
    return len(text)


def _matching(text: str, i: int) -> int:
    """Index of the bracket that closes the opener at ``i`` (``len(text)`` when it never closes)."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            i = _skip_quoted(text, i)
            continue
        if c in _CLOSERS:
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


_DEFINITION = re.compile(r"(?<![\w$.`-])(?:function|filter)\s+[A-Za-z_$][\w:$-]*", re.I)


def _blank_functions(text: str) -> str:
    """Blank every ``function`` / ``filter`` definition (keyword, name, parameter list, param() block and body).

    Code in a definition runs only if called, in its own scope, so neither a split nor a read there counts for the
    script parameter (no call graph is followed). A definition with no body brace, or one that never closes, is
    blanked to the end of the text. Newlines are kept, so the length never changes.
    """
    parts: list[str] = []
    pos, n = 0, len(text)
    while (m := _DEFINITION.search(text, pos)) is not None:
        i = m.end()
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == "(":
            i = _matching(text, i) + 1
            while i < n and text[i].isspace():
                i += 1
        end = min(n, _matching(text, i) + 1) if i < n and text[i] == "{" else n
        parts.append(text[pos:m.start()])
        parts.append("".join(ch if ch in "\r\n" else " " for ch in text[m.start():end]))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


# A split result that is thrown away: piped into Out-Null or redirected to $null anywhere after the stage.
_DISCARDED = re.compile(r"\|\s*Out-Null\b|>\s*\$null\b", re.I)


def _stage_split(expr: str, source: re.Pattern[str]) -> int:
    """End offset (just past ``}``) of a whitelisted split stage at the start of ``expr``, else -1.

    ``source`` matches the pipeline head up to the stage block's ``{``: the parameter piped straight into
    ``ForEach-Object`` / ``%`` with nothing in between. The block body must be exactly one split expression on
    ``$_`` / ``$PSItem`` (``_STAGE_BODY``), so a second statement, an assignment or a nested stage refuses it.
    """
    m = source.match(expr)
    if not m:
        return -1
    close = _matching(expr, m.end() - 1)
    if close >= len(expr) or not _STAGE_BODY.fullmatch(expr, m.end(), close) or _DISCARDED.search(expr, close + 1):
        return -1
    return close + 1


def is_normalised(text: str, name: str, block_end: int) -> bool:
    """True only when a split of ``$name`` matches one of the two whitelisted forms; every other shape fails closed.

    Statements are read with every comment and string literal blanked, so neither a split nor a read inside a
    string ever counts, and with every ``function`` / ``filter`` definition blanked, so only top-level script code
    counts as a split or a read. A split assigned to ``$null`` or under a ``[void]`` cast, or whose stage output is
    piped into ``Out-Null`` or redirected to ``$null``, is never credited.

    1. TEMP/DIRECT: ``$X = $Name -split '<lit>'`` / ``$X = $Name[0].Split(...)`` (optionally inside ``@( )``), with
       ``$X`` either another variable or ``$Name`` itself, and ``$X`` later read as a bare (not backtick-escaped)
       token before it is overwritten.
    2. STAGE: ``$Name | ForEach-Object { <split of $_> }`` (or ``%``), whose block is that one split expression and
       nothing else, or exactly ``if (Test-Path -LiteralPath $_) { $_ } else { <split of $_> }``. Its result must flow on: assigned back to ``$Name``, assigned to a variable read later as in
       form 1, or piped into a further stage.

    ``$Name = Resolve-... $Name`` still counts as a normalising helper.
    """
    var = r"\$" + re.escape(name) + r"\b"
    direct = re.compile(r"(?:@\(\s*)?" + _split_of(var + r"(?:\[[^\]]*\])?") + r"(?:\s*\))?", re.I)
    stage_rhs = re.compile(r"(?:@?\(\s*)?" + var + r"\s*\|\s*(?:ForEach-Object|%)\s*\{", re.I)
    stage_stmt = re.compile(var + r"\s*\|\s*(?:ForEach-Object|%)\s*\{", re.I)
    helper = re.compile(r"Resolve-[\w-]+.*" + var, re.I)
    assign = re.compile(r"^(?:\[[^\]]*\]\s*)*\$(\w+)\s*=\s*(.*)$", re.I)
    void_cast = re.compile(r"^(?:\[[^\]]*\]\s*)*\[void\]", re.I)

    def read_before_overwrite(target: str, later: tuple[str, ...]) -> bool:
        mention = re.compile(r"(?<!`)\$" + re.escape(target) + r"\b", re.I)
        for stmt in later:
            m = assign.match(stmt)
            if m and m.group(1).lower() == target.lower():
                return mention.search(m.group(2)) is not None
            if mention.search(stmt):
                return True
        return False

    stmts = _body_statements(text, block_end)
    for k, stmt in enumerate(stmts):
        m = assign.match(stmt)
        if m:
            target, rhs = m.group(1), m.group(2)
            if target.lower() == "null" or void_cast.match(stmt):
                continue
            if target.lower() == name.lower() and (_stage_split(rhs, stage_rhs) >= 0 or helper.match(rhs)):
                return True
            if (direct.fullmatch(rhs) or _stage_split(rhs, stage_rhs) >= 0) and read_before_overwrite(target, stmts[k + 1:]):
                return True
        else:
            end = _stage_split(stmt, stage_stmt)
            if end >= 0 and stmt[end:].lstrip().startswith("|") and not stmt[end:].lstrip().startswith("||"):
                return True
    return False


def _logical_commands(text: str) -> list[tuple[int, str]]:
    """Join backtick / backslash / trailing-comma continuation lines; keep the first physical line number.

    Comma continuation is what keeps a multi-line ``@('-File', $script, '-P', $value)`` argument array in one
    command. It is capped so a comma-terminated data file cannot become one giant command.
    """
    cmds: list[tuple[int, str]] = []
    cur, start, joined = "", 0, 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not cur:
            start, joined = no, 0
        stripped = raw.rstrip()
        if stripped.endswith("`") or stripped.endswith("\\"):
            cur += stripped[:-1] + " "
            continue
        if stripped.endswith(",") and joined < MAX_COMMA_JOIN:
            cur += stripped + " "
            joined += 1
            continue
        cmds.append((start, cur + raw))
        cur = ""
    if cur:
        cmds.append((start, cur))
    return cmds


def _array_variable_names(text: str) -> frozenset[str]:
    """Names (lower-case) that the file declares or assigns as arrays, following ``$a = $b`` aliases."""
    names: set[str] = set()
    for m in re.finditer(r"\[(?:string\[\]|object\[\]|array)\]\s*`?\$(\w+)", text, re.I):
        names.add(m.group(1).lower())
    assigns = re.findall(r"`?\$(\w+)\s*\+?=\s*([^\r\n]*)", text)
    # An array value: @(...), a cast, a -split or .Split( (unless its result is indexed down to one element), a
    # comma list, or a cmdlet/helper whose NAME says Array (never a variable or member that merely contains the
    # word, such as $arrayCount).
    arrayish = re.compile(
        r"^(?:@\(|\[(?:string\[\]|object\[\]|array)\]|.*-split\b|.*\.Split\s*\([^)]*\)(?!\s*\[)"
        r"|&?\s*[A-Za-z]+-[\w-]*Array[\w-]*(?:\s|$)"
        r"|(?:'[^']*'|\"[^\"]*\"|`?\$\w+)\s*,)",
        re.I,
    )
    for lhs, rhs in assigns:
        if arrayish.match(rhs.strip()):
            names.add(lhs.lower())
    for _ in range(3):
        for lhs, rhs in assigns:
            alias = re.fullmatch(r"`?\$(\w+)", rhs.strip())
            if alias and alias.group(1).lower() in names:
                names.add(lhs.lower())
    return frozenset(names)


def _comma_value(cmd: str, name: str) -> str | None:
    item = r"(?:'[^']*'|\"[^\"]*\"|[^\s`|),]+)"
    for m in re.finditer(r"(?<![\w-])-" + re.escape(name) + r"(?:\s+|:)(" + item + r"(?:," + item + r")*)", cmd, re.I):
        if "," in m.group(1):
            return m.group(1)
    return None


def embedded_launchers(text: str) -> list[str]:
    """Bodies of single-quoted here-strings that are themselves scripts (they start with ``param(``)."""
    return re.findall(r"@'\r?\n(\s*(?:\[CmdletBinding\(\)\]\s*)?param\(.*?)\r?\n'@", text, re.S | re.I)


def validateset_string_arrays(script: str, params: list[tuple[str, int, int]] | None = None) -> list[str]:
    """``[string[]]`` parameters guarded by ``[ValidateSet(...)]``: validation runs on the one joined element
    (``-P a,b``) before any in-script split can run, so a comma list is rejected at bind time."""
    names = []
    if params is None:
        params = script_string_array_params(script)
    clean = _blank_comments(script) if params else ""
    for name, _line, end in params:
        decl = clean[max(0, end - 4000):end]
        if re.search(r"\[ValidateSet\([^)]*\)\]\s*(?:\[[^\]]*\]\s*)*\[string\[\]\]\s*\$" + re.escape(name) + r"\b", decl, re.I):
            names.append(name)
    return names


def _quoted_comma_token(cmd: str, name: str) -> str | None:
    """``'-P','a,b'`` (ArgumentList tokens): one quoted element holding a comma list."""
    m = re.search(r"(?<![\w-])-" + re.escape(name) + r"['\"]\s*,\s*('[^']*,[^']*'|\"[^\"]*,[^\"]*\")", cmd, re.I)
    return m.group(1) if m else None


def _array_value(cmd: str, name: str, array_vars: frozenset[str]) -> str | None:
    """An array variable or ``@(...)`` literal handed to ``-P`` (``-P $a`` or ``'-P',$a``).

    Under ``-File`` an array expands to separate tokens, so only its first element binds to ``-P``. A
    ``($a -join ',')`` value starts with ``(`` and is not matched; a scalar or unknown variable is not either.
    """
    pat = r"(?<![\w-])-" + re.escape(name) + r"['\"]?(?:\s*,\s*|\s+|:)\s*(@\(|`?\$(\w+)(?![\w.\[(:]))"
    for m in re.finditer(pat, cmd, re.I):
        if m.group(1) == "@(":
            return "@(...) literal"
        if m.group(2).lower() in array_vars:
            return f"array variable ${m.group(2)}"
    return None


def _caller_commands(text: str) -> list[tuple[int, str, str, bool]]:
    """(line, command, lower-cased command, token_file) for each command that can be a ``-File`` caller.

    ``token_file`` marks ``'-File', $script, '-P', $v``: an ArgumentList / argv array whose script is named
    elsewhere in the file rather than in the command itself.
    """
    out = []
    for lineno, cmd in _logical_commands(text):
        has_file = re.search(r"(?<![\w-])-File\b", cmd, re.I) is not None
        token_file = re.search(r"['\"]-File['\"]", cmd, re.I) is not None
        has_command = re.search(r"(?<![\w-])-Command\b", cmd, re.I) is not None
        usage = re.search(r"\busage\s*:", cmd, re.I) is not None
        if has_file or token_file or (usage and not has_command):
            out.append((lineno, cmd, cmd.lower(), token_file))
    return out


def find_violations(files: dict[str, str]) -> list[Violation]:
    """files maps a repo-relative posix path to its text."""
    scripts: dict[str, list[tuple[str, bool]]] = {}
    validated: set[tuple[str, str]] = set()
    for path, text in files.items():
        if not path.lower().endswith(".ps1"):
            continue
        # A top-level [ValidateSet] rejects the joined element at bind time, so no in-script split can clear it.
        declared = script_string_array_params(text)
        if not declared:
            continue
        guarded = {name.lower() for name in validateset_string_arrays(text, declared)}
        validated.update((path, name.lower()) for name in guarded)
        params = [
            (name, name.lower() not in guarded and is_normalised(text, name, end))
            for name, _line, end in declared
        ]
        if params:
            scripts[path] = params
    out: list[Violation] = []
    lowered = {path: text.lower() for path, text in files.items()}
    per_file: dict[str, tuple[frozenset[str], list[tuple[int, str, str, bool]]]] = {}
    for script, params in sorted(scripts.items()):
        base = script.rsplit("/", 1)[-1].lower()
        open_names = [name for name, normalised in params if not normalised]
        for path, text in sorted(files.items()):
            if base not in lowered[path]:
                continue
            if path not in per_file:
                per_file[path] = (_array_variable_names(text), _caller_commands(text))
            array_vars, commands = per_file[path]
            for lineno, cmd, cmd_lower, token_file in commands:
                names_script = base in cmd_lower
                if names_script:
                    for name in open_names:
                        if "-" + name.lower() not in cmd_lower:
                            continue
                        value = _comma_value(cmd, name) or _quoted_comma_token(cmd, name)
                        if value is not None:
                            why = (
                                "a [ValidateSet] rejects the joined element at bind time, before any split"
                                if (script, name.lower()) in validated
                                else "with no split"
                            )
                            out.append(Violation(script, name, path, lineno, f"comma list {value[:60]!r} reaches a [string[]] {why}"))
                if names_script or token_file:
                    for name, _normalised in params:
                        if "-" + name.lower() not in cmd_lower:
                            continue
                        shape = _array_value(cmd, name, array_vars)
                        if shape is not None:
                            out.append(Violation(script, name, path, lineno, f"{shape} reaches a [string[]] over -File (only its first element binds; join it and split in the script)"))
    return out


def tracked_texts(root: Path = REPO_ROOT) -> dict[str, str]:
    raw = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    files: dict[str, str] = {}
    for rel in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not rel or rel.startswith(SKIP_PREFIXES) or rel == SELF or Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


@functools.cache
def _live_files() -> dict[str, str]:
    return tracked_texts()


@functools.cache
def _live_found() -> tuple[Violation, ...]:
    """One scan of the live tree shared by every test that needs it (each scan reads and parses ~1k files)."""
    return tuple(find_violations(_live_files()))


class _CountingStr(str):
    """A str that counts the characters every slice and ranged search it serves touches (``work``).

    ``_statements`` indexes its input one character at a time, so a linear scan touches each character O(1) times; a
    ``body[i + 1:]`` copy or ``body.count(..., 0, i)`` per newline touches O(n) characters per line instead. Counting
    that is deterministic, unlike a wall-clock ratio, which flakes on a contended runner.
    """

    def __new__(cls, text: str) -> "_CountingStr":
        self = super().__new__(cls, text)
        self.work = 0
        return self

    def __getitem__(self, key):
        piece = super().__getitem__(key)
        self.work += max(1, len(piece))
        return piece

    def _scan(self, start, end) -> None:
        self.work += max(1, len(range(*slice(start, end).indices(len(self)))))

    def count(self, sub, start=None, end=None):
        self._scan(start, end)
        return super().count(sub, start, end)

    def find(self, sub, start=None, end=None):
        self._scan(start, end)
        return super().find(sub, start, end)

    def rfind(self, sub, start=None, end=None):
        self._scan(start, end)
        return super().rfind(sub, start, end)

    def index(self, sub, start=None, end=None):
        self._scan(start, end)
        return super().index(sub, start, end)


class PwshStringArrayFileParams(unittest.TestCase):
    RED_SCRIPT ="param(\n    [Parameter(Mandatory = $true)][string[]]$Dirs,\n    [int]$Top = 1\n)\n$Dirs | ForEach-Object { $_ }\n"

    def test_red_usage_line_with_comma_list_and_no_split_is_flagged(self):
        files = {
            "tools/x/trace.ps1": "# Usage: trace.ps1 -Dirs dirA,dirB [-Top 3]\n" + self.RED_SCRIPT,
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], [(v.script, v.param) for v in find_violations(files)])

    def test_red_file_caller_with_continuation_lines_is_flagged(self):
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "docs/how.md": "pwsh -NoProfile -File tools\\x\\trace.ps1 `\n    -Top 3 `\n    -Dirs 'a','b'\n    -Dirs a,b\n",
        }
        self.assertEqual(1, len(find_violations(files)))

    def test_green_split_in_script_clears_it(self):
        fixed = self.RED_SCRIPT.replace(
            "$Dirs | ForEach",
            "$Dirs = @($Dirs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })\n$Dirs | ForEach",
        )
        files = {"tools/x/trace.ps1": fixed, "docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        self.assertEqual([], find_violations(files))

    def test_green_dotnet_split_and_resolve_helper_clear_it(self):
        dot = self.RED_SCRIPT.replace("$Dirs | ForEach", "$Dirs = @($Dirs[0].Split(','))\n$Dirs | ForEach")
        helper = self.RED_SCRIPT.replace("$Dirs | ForEach", "$Dirs = Resolve-Dirs -Dirs $Dirs\n$Dirs | ForEach")
        for text in (dot, helper):
            files = {"tools/x/trace.ps1": text, "docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
            self.assertEqual([], find_violations(files), text)

    def test_green_single_value_command_mode_and_in_process_callers_are_not_flagged(self):
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "docs/a.md": "pwsh -File tools/x/trace.ps1 -Dirs onlyone\n",
            "docs/b.md": "pwsh -Command \"& tools/x/trace.ps1 -Dirs a,b\"\n",
            "docs/c.md": "& tools/x/trace.ps1 -Dirs a,b\n",
        }
        self.assertEqual([], find_violations(files))

    def _split_fixed(self) -> str:
        return self.RED_SCRIPT.replace(
            "$Dirs | ForEach", "$Dirs = @($Dirs | ForEach-Object { $_ -split ',' })\n$Dirs | ForEach"
        )

    def _flagged(self, files: dict[str, str]) -> list[tuple[str, str]]:
        return [(v.script, v.param) for v in find_violations(files)]

    def test_red_array_variable_to_file_child_is_flagged_even_when_the_callee_splits(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": "$dirs = @('a', 'b')\n& pwsh -NoProfile -File tools\\x\\trace.ps1 -Dirs $dirs -Top 3\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_quoted_argumentlist_tokens_with_array_variable_are_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": "$Dirs = @('a', 'b')\nStart-Process pwsh -ArgumentList '-File','tools/x/trace.ps1','-Dirs',$Dirs\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_multiline_argument_array_naming_the_script_elsewhere_is_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": (
                "$validator = Join-Path $repo 'tools\\x\\trace.ps1'\n$dirs = @('a', 'b')\n$argv = @(\n"
                "    '-NoProfile',\n    '-File',\n    $validator,\n    '-Dirs',\n    $dirs\n)\n"
            ),
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_array_literal_and_quoted_comma_token_callers_are_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "docs/lit.md": "pwsh -File tools/x/trace.ps1 -Dirs @('a','b')\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "tools/x/run.ps1": "Start-Process pwsh -ArgumentList '-File','tools/x/trace.ps1','-Dirs','a,b'\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_green_joined_scalar_and_unknown_variable_callers_are_not_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": (
                "$dirs = @('a', 'b')\n$one = 'a'\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs ($dirs -join ',')\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs $one\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs $unknown\n"
            ),
        }
        self.assertEqual([], self._flagged(files))

    def test_red_unrelated_split_beside_the_parameter_does_not_clear_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        for nearby in (
            "Write-Host $Dirs\n$y = $z -split ';'\n$Dirs | ForEach",
            "Write-Host $Dirs; $y = $z -split ';'\n$Dirs | ForEach",
            "$Dirs = $env:DIRS -split ','\n$Dirs | ForEach",
        ):
            files = {"tools/x/trace.ps1": self.RED_SCRIPT.replace("$Dirs | ForEach", nearby), **callers}
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files), nearby)

    def test_green_multiline_normalisation_bound_to_the_parameter_clears_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        for fixed in (
            "$Dirs = @($Dirs |\n    ForEach-Object { $_ -split ',' } |\n    Where-Object { $_ })\n$Dirs | ForEach",
            "$Dirs = @($Dirs | ForEach-Object {\n    $_ -split ','\n})\n$Dirs | ForEach",
            "$Dirs = $Dirs `\n    -split ','\n$Dirs | ForEach",
        ):
            files = {"tools/x/trace.ps1": self.RED_SCRIPT.replace("$Dirs | ForEach", fixed), **callers}
            self.assertEqual([], self._flagged(files), fixed)

    def test_red_split_on_something_else_or_never_used_does_not_clear_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        for unrelated in (
            "$Dirs | ForEach-Object { $y = $z -split ',' }\n",
            "$Dirs = @($Dirs | ForEach-Object { $z -split ',' })\n",
            "$Dirs | ForEach-Object { $z -split ',' } | ForEach-Object { $_ }\n",
            "$parts = $Dirs[0].Split(',')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ }\n$other = $z -split ','\n",
        ):
            files = {"tools/x/trace.ps1": self.RED_SCRIPT.split("$Dirs | ForEach")[0] + unrelated, **callers}
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files), unrelated)

    def test_green_split_result_that_flows_to_a_later_use_clears_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        for fixed in (
            "$Dirs = @($Dirs[0].Split(','))\n$Dirs | ForEach-Object { $_ }\n",
            "$parts = $Dirs[0].Split(',')\n$parts | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { ([string]$_) -split ',' } | ForEach-Object { $_.Trim() }\n",
        ):
            self.assertEqual([], self._flagged({"tools/x/trace.ps1": head + fixed, **callers}), fixed)

    def test_red_split_temporary_overwritten_before_any_read_does_not_clear_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        for unread in (
            "$parts = $Dirs[0].Split(',')\n$parts = @()\n$Dirs | ForEach-Object { $_ }\n",
            "$parts = $Dirs | ForEach-Object { $_ -split ',' }\n$parts = $null\n$Dirs | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + unread, **callers}), unread)
        for read in (
            "$parts = $Dirs[0].Split(',')\nWrite-Host $parts\n$parts = @()\n$Dirs | ForEach-Object { $_ }\n",
            "$parts = $Dirs[0].Split(',')\n$parts = @($parts | Where-Object { $_ })\n$parts | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([], self._flagged({"tools/x/trace.ps1": head + read, **callers}), read)

    def test_red_split_on_an_inner_pipelines_item_does_not_clear_the_outer_parameter(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        for nested in (
            "$Dirs | ForEach-Object { 'png,jpg' | ForEach-Object { $_ -split ',' } | Out-Null; $_ } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $ext | ForEach-Object { $_.Split(',') } | Out-Null\n $_ } | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + nested, **callers}), nested)

    def test_red_any_nested_pipeline_in_the_consuming_stage_refuses_the_credit(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        tail = " | ForEach-Object { $_ }\n"
        for nested in (
            # a valued switch or an explicit InputObject on the inner stage
            "$Dirs | ForEach-Object { 'png,jpg' | ForEach-Object -ErrorAction Stop { $_ -split ',' } | Out-Null; $_ }" + tail,
            "$Dirs | ForEach-Object { ForEach-Object -InputObject 'png,jpg' { $_ -split ',' } | Out-Null; $_ }" + tail,
            "$Dirs | ForEach-Object { 'png,jpg' | % { $_ -split ',' } | Out-Null; $_ }" + tail,
            "$Dirs | ForEach-Object { 'png,jpg' | Where-Object { $_.Split(',') } | Out-Null; $_ }" + tail,
            "$Dirs | ForEach-Object { 'png,jpg'.ForEach({ $_ -split ',' }) | Out-Null; $_ }" + tail,
            # the split is outside the nested pipeline, but the stage block still nests one: refused, not resolved
            "$Dirs | ForEach-Object { 'png' | Out-Null; $_ -split ',' }" + tail,
            "$Dirs | ForEach-Object { 'x' | ForEach-Object { $_ }; $_ -split ',' }" + tail,
            # other constructs that rebind $_
            "$Dirs | ForEach-Object { switch ('png') { default { $_ -split ',' } } ; $_ }" + tail,
            "$Dirs | ForEach-Object { try { 1 } catch { $_ -split ',' } ; $_ }" + tail,
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + nested, **callers}), nested)
        # r4 whitelist: these cleared the parameter through r3 and are flagged now (moved here, not deleted). A
        # stage earns credit only when its block is one statement that is the split itself, and only when it takes
        # the parameter directly; a temporary earns it only when read as a bare token outside every string.
        for outside in (
            # was the green `bound` case of the inner-pipeline test: the split sits in an if/else branch
            "$Dirs | ForEach-Object { if ($_) { $_ -split ',' } else { $_ } }" + tail,
            # was the green `passthrough` case of the downstream test: a stage sits between $Dirs and the split
            "$Dirs | ForEach-Object { $_.Trim() } | ForEach-Object { $_ -split ',' }" + tail,
            # was the green double-quoted `read` case of the literal test: the only read is inside "..."
            "$parts = $Dirs[0].Split(',')\nWrite-Verbose \"Split result is in $parts\"\n$parts = @()\n$Dirs | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + outside, **callers}), outside)
        clean = "$Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } }" + tail
        self.assertEqual([], self._flagged({"tools/x/trace.ps1": head + clean, **callers}), clean)
        for guarded_other in (
            "$Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ -split ',' } else { $_ } }" + tail,
            "$Dirs | ForEach-Object { if (Test-Path -LiteralPath $z) { $_ } else { $_ -split ',' } }" + tail,
            "$Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $parts = $_ -split ','; $_ } }" + tail,
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + guarded_other, **callers}), guarded_other)

    def test_red_variable_mentioned_only_in_a_literal_is_not_a_read(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        overwrite = "$parts = @()\n$Dirs | ForEach-Object { $_ }\n"
        split = "$parts = $Dirs[0].Split(',')\n"
        for mention in (
            "Write-Verbose 'Split result is in $parts'\n",
            "$note = @'\nSplit result is in $parts\n'@\n",
            "# Split result is in $parts\n",
            "<# Split result is in $parts #>\n",
            "Write-Verbose 'it''s in $parts'\n",
        ):
            text = split + mention + overwrite
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + text, **callers}), text)
        for fake in (
            "$Dirs | ForEach-Object { 'x $_ -split y' }\n",
            "$Dirs = 'a $Dirs -split b'\n$Dirs | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + fake, **callers}), fake)

    def test_red_item_split_downstream_of_an_unrelated_split_does_not_clear_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        unrelated = "$Dirs | ForEach-Object { $z -split ',' } | ForEach-Object { $_ -split ';' } | ForEach-Object { $_ }\n"
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({"tools/x/trace.ps1": head + unrelated, **callers}), unrelated)

    def _flag_body(self, body: str) -> list[tuple[str, str]]:
        head = self.RED_SCRIPT.split("$Dirs | ForEach")[0]
        return self._flagged({"tools/x/trace.ps1": head + body, "docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"})

    def test_red_split_stage_with_a_second_statement_does_not_clear_it(self):
        # sol r3 SPLIT-DATAFLOW-STAGE-UNUSED-1: the stage splits into an unread local and emits the item unchanged.
        for body in (
            "$Dirs | ForEach-Object { $parts = $_ -split ','; $_ } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object {\n    $parts = $_ -split ','\n    $_\n} | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_split_temporary_read_only_inside_a_string_does_not_clear_it(self):
        # sol r3 SPLIT-DATAFLOW-ESCAPED-READ-1: a backtick-escaped mention in "..." is text, not a read.
        body = "$parts = $Dirs[0].Split(',')\nWrite-Verbose \"Split result is in `$parts\"\n$parts = @()\n$Dirs | ForEach-Object { $_ }\n"
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_parenless_intrinsic_foreach_in_the_stage_does_not_clear_it(self):
        # fable r3 NESTED-PARENLESS-INTRINSIC-METHOD-1: .ForEach{ } / .Where{ } rebind $_ with no pipe or cmdlet.
        for body in (
            "$Dirs | ForEach-Object { $null = 'png,jpg'.ForEach{ $_ -split ',' }; $_ } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $null = 'png,jpg'.Where{ $_ -split ',' }; $_ } | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_where_object_stage_split_does_not_clear_it(self):
        # fable r3 WHERE-STAGE-SPLIT-CREDITED-1: a filter discards the split result; only ForEach-Object / % count.
        for body in (
            "$Dirs | Where-Object { $_ -split ',' } | ForEach-Object { $_ }\n",
            "$Dirs | ? { $_ -split ',' } | ForEach-Object { $_ }\n",
            "$Dirs | Sort-Object { $_ -split ',' } | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_split_text_in_a_double_quoted_string_does_not_clear_it(self):
        # fable r3 hardening DOUBLE-QUOTED-SPLIT-TEXT-CREDITED-1: split text inside "..." is not a split.
        for body in (
            "$Dirs = \"a $Dirs -split b\"\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { Write-Host \"$_ -split ','\"; $_ } | ForEach-Object { $_ }\n",
            "$Dirs = @\"\n$Dirs -split ','\n\"@\n$Dirs | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_blockless_stage_before_the_split_does_not_clear_it(self):
        # fable r3 hardening STREAM-CARRY-BLOCKLESS-STAGE-1: the split stage must take the parameter directly.
        for body in (
            "$parts = $Dirs | Get-Item | ForEach-Object { $_ -split ',' }\n$parts | Out-Null\n",
            "$Dirs | Select-Object -First 1 | ForEach-Object { $_ -split ',' } | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_green_each_whitelisted_form_clears_it(self):
        for body in (
            # TEMP/DIRECT form: split of the parameter, then a bare read outside any string
            "$parts = $Dirs[0].Split(',')\nWrite-Verbose $parts\n$parts = @()\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ','\n$Dirs | ForEach-Object { $_ }\n",
            # STAGE form: the parameter piped straight into a one-statement split block
            "$Dirs | % { $PSItem.Split(',').Trim() } | ForEach-Object { $_ }\n",
            "$parts = $Dirs | ForEach-Object {\n    $_ -split ','\n}\n$parts | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([], self._flag_body(body), body)

    def test_red_split_that_does_not_split_on_a_comma_does_not_clear_it(self):
        # SPLIT-DELIMITER-NOT-CHECKED-1 (fable r5 on #321, sol r3): `pwsh -File x.ps1 -Dirs a,b` hands the script ONE
        # element "a,b"; only a split on a comma separates it (native: a ';' split leaves Count=1).
        for body in (
            "$Dirs = $Dirs -split ';'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split \"`n\"\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',1\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(';')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(',', 1)\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | % { $_ -split ';' } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_.Split(';') } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ -split ',',1 } | ForEach-Object { $_ }\n",
            "$Dirs = @($Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ';' } })\n",
            "$Dirs = $Dirs -split ', '\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',,'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ''\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split \"$sep\"\n$Dirs | ForEach-Object { $_ }\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_green_split_on_exactly_one_comma_clears_it(self):
        # r2 whitelist: ',' (optionally padded by \s* / ' *'), limit absent or 0; .Split with exactly one comma and
        # nothing else. Every row was run natively in pwsh and splits `-Dirs a,b,c` into three elements.
        for body in (
            "$Dirs = $Dirs -split '\\s*,\\s*'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ' *, *'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '\\s*,'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split \",\"\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',0\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split([char]',')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(@(','))\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split([char[]]',')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split([char[]]@(','))\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(@([char]','))\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ -split '\\s*,\\s*' } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_.Split([char]',') } | ForEach-Object { $_ }\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([], self._flag_body(body), body)

    def test_red_conditional_or_non_comma_regex_does_not_clear_it(self):
        # SPLIT-DELIMITER-NOT-CHECKED-1 r2 (sol r1): `',(?=[a-z])'` splits `a,b` but leaves `a,1` whole (native Count=1),
        # so a regex earns no credit because it behaves on one sample. Only ',' padded by \s* / ' *' is credited.
        for body in (
            "$Dirs = $Dirs -split ',(?=[a-z])'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',(?![0-9])'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',|;'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '[,;]'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '[,]'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '(,)'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '^,'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',+'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '\\s+,\\s+'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '\\,'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split \"\\,\"\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '\\x2c'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',0,'SimpleMatch'\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ -split ',(?=[a-z])' } | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ -split '[,;]' } | ForEach-Object { $_ }\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_positive_split_limit_does_not_clear_it(self):
        # fable r1 SPLIT-LIMIT-PARTIAL-JOIN-1: `-split ',',2` on `-Dirs a,b,c` gives Count=2, [a][b,c] (native). The
        # caller's list length is unknown, so only an absent limit or 0 is credited.
        for body in (
            "$Dirs = $Dirs -split ',',2\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',3\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',10\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split ',',-1\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_ -split ',',2 } | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs -split '\\s*,\\s*',2\n$Dirs | ForEach-Object { $_ }\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_dotnet_split_with_extra_arguments_does_not_clear_it(self):
        # fable r1 DOTNET-SPLIT-EXTRA-SEPARATORS-1 / sol r1 DOTNET-SPLIT-NATIVE-OVERLOAD-1: `.Split('x', ',')` and
        # `.Split(';', ',')` throw natively (the second argument converts to Int32), so they were never a comma split.
        # Only a Split whose single argument is one comma is credited; a count or an option is refused.
        for body in (
            "$Dirs = $Dirs[0].Split('x', ',')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(';', ',')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(',', 3)\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(',', [StringSplitOptions]::RemoveEmptyEntries)\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(@(',', ';'))\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split(',', ';')\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs = $Dirs[0].Split([char[]]@(',', 'x'))\n$Dirs | ForEach-Object { $_ }\n",
            "$Dirs | ForEach-Object { $_.Split('x', ',') } | ForEach-Object { $_ }\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_split_temporary_mentioned_only_inside_a_function_does_not_clear_it(self):
        # sol r5 SPLIT-DATAFLOW-FUNCTION-SCOPE-1: a function's name, parameter list, param() block and body are not reads.
        split = "$parts = $Dirs[0].Split(',')\n"
        tail = "$Dirs | ForEach-Object { $_ }\n"
        for function in (
            "function Write-Parts([string[]]$parts) { $parts }\n",
            "function Write-Parts {\n    param([string[]]$parts)\n    $parts\n}\n",
            "function Show-Parts { Write-Host $parts }\n",
            "filter Show-Parts { $parts }\n",
            "function $parts { 1 }\n",
        ):
            body = split + function + tail
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)
        # a top-level read still counts with a function defined beside it
        body = split + "function Show-Parts { Write-Host $parts }\nWrite-Verbose $parts\n" + tail
        self.assertEqual([], self._flag_body(body), body)

    def test_red_split_wholly_inside_an_uncalled_function_does_not_clear_it(self):
        # sol r5 SPLIT-DATAFLOW-FUNCTION-SCOPE-1, second variant: a split inside a definition never normalises the parameter.
        tail = "$Dirs | ForEach-Object { $_ }\n"
        for function in (
            "function Get-Parts {\n    $parts = $Dirs[0].Split(',')\n    $parts\n}\n",
            "function Get-Parts { $Dirs = $Dirs -split ','\n$Dirs | ForEach-Object { $_ } }\n",
            "function Get-Parts([string[]]$Dirs) { $Dirs = @($Dirs | ForEach-Object { $_ -split ',' }) }\n",
            "filter Get-Parts { $Dirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_ } }\n",
        ):
            body = function + tail
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)
        body = "$Dirs = $Dirs -split ','\nfunction Get-Parts { 1 }\n" + tail
        self.assertEqual([], self._flag_body(body), body)

    def test_red_split_assigned_to_null_is_never_credited(self):
        # fable r5 NULL-TARGET-SKIP-UNTESTED-1: a later `$null` mention must not read a split discarded into $null.
        body = "$null = $Dirs -split ','\n$x = $null\n$Dirs | ForEach-Object { $_ }\n"
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_split_stage_whose_output_is_discarded_does_not_clear_it(self):
        # fable r5 STAGE-PIPED-TO-OUT-NULL-CREDITED-1: a stage piped into Out-Null, assigned to $null or cast to
        # [void] discards the split result.
        for body in (
            "$Dirs | ForEach-Object { $_ -split ',' } | Out-Null\n",
            "$Dirs | % { $_ -split ',' } | ForEach-Object { $_.Trim() } | Out-Null\n",
            "$Dirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_ } > $null\n",
            "$Dirs = $Dirs | ForEach-Object { $_ -split ',' } | Out-Null\n$Dirs | ForEach-Object { $_ }\n",
            "$parts = $Dirs | ForEach-Object { $_ -split ',' } | Out-Null\n$parts | ForEach-Object { $_ }\n",
            "$null = $Dirs | ForEach-Object { $_ -split ',' }\n$x = $null\n$Dirs | ForEach-Object { $_ }\n",
            "[void]($Dirs | ForEach-Object { $_ -split ',' })\n$Dirs | ForEach-Object { $_ }\n",
            "[void]$parts = $Dirs -split ','\n$parts | ForEach-Object { $_ }\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flag_body(body), body)

    def test_red_dotnet_split_array_variable_to_file_child_is_flagged(self):
        script = {"tools/x/trace.ps1": self._split_fixed()}
        call = "& pwsh -File tools\\x\\trace.ps1 -Dirs $n\n"
        for assign in ("$n = $raw.Split(',')\n", "$n = 'a,b'.Split(',', 2)\n"):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({**script, "tools/x/run.ps1": assign + call}), assign)
        for scalar in ("$n = $raw.Split(',')[0]\n", "$n = $raw.Trim()\n"):
            self.assertEqual([], self._flagged({**script, "tools/x/run.ps1": scalar + call}), scalar)

    def test_red_array_named_scalar_is_not_an_array_but_an_array_helper_is(self):
        script = {"tools/x/trace.ps1": self._split_fixed()}
        call = "& pwsh -File tools\\x\\trace.ps1 -Dirs $n\n"
        for assign in ("$n = $arrayCount\n", "$n = $myArray.Count\n", "$n = $rawArrayText\n"):
            self.assertEqual([], self._flagged({**script, "tools/x/run.ps1": assign + call}), assign)
        for assign in (
            "$n = Convert-ToPowerShellArrayLiteral $raw\n",
            "$n = [string[]]$raw\n",
            "$n = 'a', 'b'\n",
            "$n = @($raw)\n",
        ):
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged({**script, "tools/x/run.ps1": assign + call}), assign)

    def test_red_later_comma_list_is_not_masked_by_an_earlier_mention(self):
        two = "param(\n    [string[]]$Dirs,\n    [string[]]$Names\n)\n$Dirs | ForEach-Object { $_ }\n$Names | ForEach-Object { $_ }\n"
        for text in (
            "pwsh -File tools/x/two.ps1 -Dirs one -Names a,b\n",
            "see -Dirs one,\npwsh -File tools/x/two.ps1 -Dirs a,b\n",
            "pwsh -File tools/x/two.ps1 -Dirs one -Dirs a,b\n",
        ):
            found = self._flagged({"tools/x/two.ps1": two, "docs/how.md": text})
            self.assertEqual(1, len(found), text)

    def test_red_top_level_validateset_on_a_string_array_flags_a_comma_list_caller(self):
        script = "param(\n    [ValidateSet('a', 'b')]\n    [string[]]$Codecs = @('a')\n)\n$Codecs = @($Codecs | ForEach-Object { $_ -split ',' })\n"
        files = {"tools/x/enc.ps1": script, "docs/how.md": "pwsh -File tools/x/enc.ps1 -Codecs a,b\n"}
        self.assertEqual([("tools/x/enc.ps1", "Codecs")], self._flagged(files))
        self.assertIn("ValidateSet", find_violations(files)[0].detail)
        files["docs/how.md"] = "pwsh -File tools/x/enc.ps1 -Codecs a\n"
        self.assertEqual([], self._flagged(files))
        plain = script.replace("    [ValidateSet('a', 'b')]\n", "")
        files = {"tools/x/enc.ps1": plain, "docs/how.md": "pwsh -File tools/x/enc.ps1 -Codecs a,b\n"}
        self.assertEqual([], self._flagged(files))

    def test_known_open_reasons_describe_what_the_callee_really_does(self):
        files = _live_files()
        gui, profile = (KNOWN_OPEN[("tools/profiling/" + n, p)] for n, p in (
            ("run-release-gui-smoke.ps1", "ExtraEnvironment"), ("run-release-playback-profile.ps1", "AdditionalArgs")))
        self.assertIn("ONE element", gui)
        self.assertIn("ValueFromRemainingArguments", files["tools/profiling/run-release-gui-smoke.ps1"])
        self.assertIn("binds positionally to ExePath", gui)
        self.assertNotIn("the script throws", gui)
        self.assertIn("positionally", profile)
        self.assertIn("$args", profile)
        self.assertNotIn("CmdletBinding", files["tools/profiling/run-release-playback-profile.ps1"])
        for reason in KNOWN_OPEN.values():
            self.assertIn("PWSH-FILE-ARRAY-PASSTHRU-1", reason)

    def test_live_top_level_validateset_script_is_seen(self):
        path = "tools/profiling/invoke-ultramagnus-cdng-export-evidence.ps1"
        self.assertEqual(["CdngCodecs"], validateset_string_arrays(_live_files()[path]))

    def test_statement_splitter_scales_linearly(self):
        # Counts the characters _statements touches instead of timing it (a wall-clock ratio flakes on a contended
        # runner): linear is exactly 4.0x for 4x the lines, the per-newline `body[i + 1:]` copy of #313 is ~16x.
        def work(lines: int) -> int:
            body = _CountingStr("Write-Host $x\n" * lines)
            self.assertEqual(["Write-Host $x"] * lines, _statements(body))
            return body.work

        small, large = work(2_000), work(8_000)
        self.assertGreater(small, 0, "_statements no longer reads through slicing or ranged search; extend _CountingStr")
        self.assertLessEqual(large, small * 5, f"4x the lines touched {large / small:.1f}x the characters (linear is 4.0x, quadratic ~16x)")

    def test_reverting_each_guarded_311_fix_in_memory_is_flagged(self):
        files = _live_files()
        self.assertEqual([], [v for v in _live_found() if (v.script, v.param) not in KNOWN_OPEN])
        fixes = (
            (
                "tools/profiling/export-release-cuda-dogfood-kit.ps1",
                "-CdngCodecs ($DngCodecs -join ',') `\n",
                "-CdngCodecs $DngCodecs `\n",
                ("tools/profiling/run-local-cuda-playback-dng-smoke.ps1", "CdngCodecs"),
            ),
            (
                "tools/profiling/invoke-ultramagnus-p3-evidence.ps1",
                "(@(`$evidenceGitStatus) -join [string][char]10),",
                "`$evidenceGitStatus,",
                ("tools/profiling/run-ultramagnus-p3-validation.ps1", "EvidenceGitStatus"),
            ),
            (
                "tools/profiling/filmstrip-balance-trace.ps1",
                "$Dirs = @($Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })",
                "",
                ("tools/profiling/filmstrip-balance-trace.ps1", "Dirs"),
            ),
        )
        for path, fixed, reverted, expected in fixes:
            self.assertIn(fixed, files[path], f"{path}: the #311 fix moved; update this test with it")
            mutated = dict(files)
            mutated[path] = files[path].replace(fixed, reverted)
            self.assertIn(expected, self._flagged(mutated), path)

    def test_red_validateset_on_a_string_array_is_seen_and_a_split_first_launcher_is_not(self):
        red = "param(\n    [ValidateSet('a', 'b')]\n    [string[]]$Codecs = @('a')\n)\n"
        green = "param(\n    [string[]]$Codecs = @('a')\n)\n$Codecs = @($Codecs | ForEach-Object { $_ -split ',' })\n"
        self.assertEqual(["Codecs"], validateset_string_arrays(red))
        self.assertEqual([], validateset_string_arrays(green))
        self.assertEqual([red.rstrip("\n")], embedded_launchers("$s = @'\n" + red + "'@\n"))

    def test_generated_launchers_split_a_string_array_before_validating_it(self):
        kit = _live_files()["tools/profiling/export-release-cuda-dogfood-kit.ps1"]
        launchers = embedded_launchers(kit)
        self.assertEqual(1, len(launchers), "the kit script embeds RUN-CUDA-DOGFOOD.ps1 as a here-string")
        for script in launchers:
            self.assertEqual([], validateset_string_arrays(script))
            for name, _line, end in script_string_array_params(script):
                self.assertTrue(is_normalised(script, name, end), name)

    def test_param_block_reader_ignores_comments_functions_and_nested_parens(self):
        text = (
            "[CmdletBinding()]\nparam(\n    # [string[]]$Commented,\n"
            "    [ValidatePattern('^(a|b)$')][string]$Plain,\n    [string[]]$Real = @('x')\n)\n"
            "function F {\n    param([string[]]$InFunction)\n}\n"
        )
        self.assertEqual(["Real"], [name for name, _line, _end in script_string_array_params(text)])

    def test_live_tree_has_no_unexplained_violations(self):
        found = _live_found()
        unexplained = [v for v in found if (v.script, v.param) not in KNOWN_OPEN]
        self.assertEqual([], unexplained, "\n".join(v.render() for v in unexplained))

    def test_non_comma_lists_are_still_uncredited_and_unflagged(self):
        files = _live_files()
        flagged = {(v.script, v.param) for v in _live_found()}
        for (path, name), why in NON_COMMA_LISTS.items():
            with self.subTest(script=path, param=name):
                declared = {n: end for n, _line, end in script_string_array_params(files[path])}
                self.assertIn(name, declared, f"{path} no longer declares -{name}: delete this NON_COMMA_LISTS entry")
                self.assertFalse(is_normalised(files[path], name, declared[name]), f"now splits on a comma? {why}")
                self.assertNotIn((path, name), flagged, "a caller now passes a comma list: split on a comma in the script")

    def test_known_open_entries_are_still_violations(self):
        still = {(v.script, v.param) for v in _live_found()}
        stale = sorted(set(KNOWN_OPEN) - still)
        self.assertEqual([], stale, f"fixed? delete these KNOWN_OPEN entries: {stale}")


if __name__ == "__main__":
    unittest.main()
