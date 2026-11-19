"""Guard PlayerTransportToolbar.h against Qt-version-specific API with no version branch.

PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1 r2 (PR 857; CoS advisory record c5033af): Qt 5 is a documented,
supported kit (docs/11-build-macos-linux.md; .github/workflows/macOS-Intel.yml builds the macOS
Intel release with qt@5), but no PR check compiles it, because that workflow runs only on
workflow_dispatch. r1's mirroredIcon() called ``QImage::flipped()``, which exists only in Qt 6.9+,
and MainWindow.cpp includes the header, so the Qt 5 release kit failed to compile unseen. This test
reads the header's text and fails on any call to an API newer than Qt 5.15 that is not inside an
arm of a ``QT_VERSION`` check that compiles only on a Qt new enough for it. Same defect class as
the QMouseEvent::position() guard of PR 856 r3 (test_qt6_only_api_guard.py on that branch).
The scope now includes GpuDisplayWindow.cpp (card PLAYER-TOOLBAR-QT5-GPUDISPLAY-FLIPPED-1), and a
tripwire fails when any other file under platform/qt or src reaches a QImage::flipped/flip name.

GRAMMAR (r5, fail closed; hub STOP RULE 20261119T0912Z). The scanner reads a file only where it
is certain what a compiler sees. Anything else it REFUSES: the file is reported unprovable (one
``REFUSED <kind>`` finding per refused form), which fails the test, and nothing else in it is read.

Read:
  * comments (``//``, ``/* */``) and the bodies of ordinary string and character literals are
    blanked; an unterminated ' or " ends at the line end; a ' inside a pp-number is lexed as a
    digit separator (and then refused, below); CR, LF and CRLF each end a line;
  * every identifier token that IS an API name of NEWER_THAN_QT5_API is a use, wherever it
    stands: a member access (also one split across lines), a qualified name, a macro argument,
    a #define body, a conditional directive or a bare call. Each use is judged by the arms open
    at the physical line its name is on;
  * ``#if``/``#ifdef``/``#ifndef``/``#elif``/``#elifdef``/``#elifndef``/``#else``/``#endif``:
    only ``#if QT_VERSION >= QT_VERSION_CHECK(a, b, c)`` and ``#if QT_VERSION <
    QT_VERSION_CHECK(a, b, c)``, with a, b and c C preprocessor integer literals, open a trusted
    arm (the >= arm, or the #else of the < form); every other condition and every #elif,
    #elifdef and #elifndef arm is untrusted;
  * the other directives it models are ``#define``, ``#undef``, ``#include``, ``#pragma``,
    ``#error``, ``#warning``, ``#line`` and the null directive ``#``.

Refused, never read:
  * a backslash-newline splice anywhere in the file, comments and literals included, also with
    spaces or tabs before the line break (adversary r4 pass 6 A1 and every splice sibling);
  * a trigraph (``??=``, ``??/``, ...) anywhere in the file;
  * a header-name (after ``#include``, ``__has_include(``, ``#pragma GCC dependency``, ...) whose
    ``<...>`` holds ``/*``, ``//``, a quote or a backslash, or whose ``"..."`` holds a backslash,
    and ``__has_include`` and its kin in a #define (adversary r5 pass 8 A1); this is checked on
    the raw text and again in the lexer, on the comment-blanked logical line, so a comment that
    spans lines before the header-name does not hide it (adversary r5 pass 9 A1);
  * a digraph (``<:``, ``:>``, ``<%``, ``%>``, ``%:``) in code;
  * a raw string literal (``R"``, ``u8R"``, ...): its d-chars and body are not modelled;
  * a backslash in code outside a literal (a universal-character-name can spell a name);
  * a token paste (``##``) in code;
  * macro indirection: a #define whose replacement list names a macro this file #defines
    (adversary r4 pass 6 A2: ``#define INDIRECT APPLY``);
  * a #define or #undef of QT_VERSION or QT_VERSION_CHECK;
  * any other directive name (``#include_next``, ``#import``, ``#embed``, a ``# 12 "f"`` line
    marker, ...: adversary r5 A2), and a C++20 ``import``/``module`` line, which can import macros;
  * a character in code outside printable ASCII, tab, VT and FF (a BOM, NBSP, U+2028, a control
    character, a non-ASCII identifier: adversary r5 A1);
  * ``control-byte`` (r7, sol r6 B1): a NUL or any other C0 control byte but tab, LF, CR, VT and
    FF, or DEL, anywhere in the raw text, comments and literals included, before lexing. GCC reads
    NUL as whitespace before a spliced line break, so ``\\`` + NUL + newline is a splice to it; the
    scanner does not model that and refuses the byte instead;
  * an ``include_alias`` token anywhere in the raw text (r7, fable r6 H2), as ``header-name``: its
    arguments are header-names the lexer does not know;
  * a ``$`` in code (r7, fable r6 H3): compilers accept it in identifiers by default, so it moves
    where a pp-number ends;
  * a digit separator in code (``1'000``; card PLAYER-TOOLBAR-QT5-GPUDISPLAY-FLIPPED-1 r2): the Qt 5
    Linux kit compiles C++11 (MLVApp.pro:176), where the ' ends the number and opens a char literal.

Non-promises: the scanner does not preprocess. It does not follow #include, does not expand a
macro defined in another file (an external macro that pastes pieces into an API name is not
seen), evaluates no #if but the two trusted forms, and reads no file that has a refused form.
It proves one thing about the scoped headers: no API name token stands outside a trusted arm.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
# Index 0 stays PlayerTransportToolbar.h: _header_with_branch and two tests read SCOPED_HEADERS[0].
SCOPED_HEADERS = (
    ROOT / "platform" / "qt" / "PlayerTransportToolbar.h",
    ROOT / "platform" / "qt" / "GpuDisplayWindow.cpp",
)

# API -> the first Qt (major, minor) that has it.
NEWER_THAN_QT5_API = {
    "flipped": (6, 9),  # QImage::flipped(Qt::Orientations)
    "flip": (6, 9),  # QImage::flip(Qt::Orientations)
    "position": (6, 0),  # QSinglePointEvent / QMouseEvent accessors
    "globalPosition": (6, 0),
    "scenePosition": (6, 0),
}
_NAMES = "|".join(sorted(NEWER_THAN_QT5_API, key=len, reverse=True))
# r5: every identifier token that IS an API name is a use, however it is reached: `.`/`->`, any
# qualified name, a line break inside the access, a macro argument, a #define body or a bare call.
# One token rule replaces the r4 member-access matcher and the macro-argument walk, so no macro
# shape (an alias of a function-like macro included, adversary r4 pass 6 A2) can carry the name
# past it.
API_NAME = re.compile(rf"\b({_NAMES})\b")
IDENTIFIER = re.compile(r"\b[^\W\d]\w*")
DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$")
# Any other directive name is refused (adversary r5 A2: `#elifdef` was not read); `#` alone is the
# null directive.
DIRECTIVE_NAME = re.compile(r"^\s*#\s*(\w*)")
MODELLED_DIRECTIVES = frozenset((
    "if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif",
    "define", "undef", "include", "pragma", "error", "warning", "line", "",
))
# Every directive pattern is anchored at the line start and `\s` is wider than a compiler's
# whitespace, so code holds printable ASCII, tab, VT and FF only (adversary r5 A1: a BOM).
NOT_PRINTABLE_ASCII = re.compile(r"[^\t\v\f\x20-\x7e]")
# A C++20 module or header-unit import is a directive without `#` and can import macros.
MODULE_DIRECTIVE = re.compile(r"^\s*(?:export\s+)?(?:import|module)\b")
# Group 3 is the replacement list; a `(` straight after the name opens a parameter list.
DEFINE = re.compile(r"^\s*#\s*define\s+(\w+)(\([^)]*\))?(.*)$")
VERSION_MACRO_REDEFINED = re.compile(r"^\s*#\s*(?:define|undef)\s+QT_VERSION(?:_CHECK)?\b")
# Refused in the raw text, anywhere (comments and literals included): translation phases 1-2
# are what the scanner does not model, so it refuses every input where they change the text, as
# far as the forms below reach. SPLICE allows only the ISO whitespace before the line break; a
# compiler that reads more as whitespace there (GCC: NUL) is covered by CONTROL_BYTE (r7, sol r6
# B1), which refuses every C0 control byte but tab, LF, CR, VT and FF, and DEL.
SPLICE = re.compile(r"\\[ \t\f\v]*\n")
CONTROL_BYTE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
# `#pragma include_alias(<a>, <b>)` (Microsoft extension) takes header-names in a context the
# lexer does not track; any include_alias token is refused (r7, fable r6 H2).
INCLUDE_ALIAS = re.compile(r"\binclude_alias\b")
TRIGRAPH = re.compile(r"\?\?[=/'()!<>\-]")
# After #include, __has_include( and the like, `<...>` is a header-name, where /*, //, quotes and
# a backslash are plain text; in `"..."` a backslash is plain text too. The lexer reads them as a
# comment, a literal or an escape, so such a header-name is refused (adversary r5 pass 8 A1).
HEADER_NAME_UNREADABLE = re.compile(
    r"\b(?:\w*include\w*|__has_\w+|dependency|import|\w*embed\w*)\b[^\n<\"]*"
    r"(?:<[^>\n]*(?:/[*/]|['\"\\])|\"[^\"\n]*\\)"
)
# The same context on the lexer's comment-blanked logical line, which a comment spanning lines
# joins (adversary r5 pass 9 A1: `__has_include /*\n*/ (<a/*>)` slipped past the raw-text regex).
# HEADER_NAME_KEYWORD ends where a header-name may start; HEADER_NAME_OPEN is inside a `<...>` one.
_HEADER_NAME_KEYWORD = r"(?:^\s*#\s*(?:\w*include\w*|\w*embed\w*)|\b__has_\w+\s*\(|\bdependency|\bimport)\s*"
HEADER_NAME_KEYWORD = re.compile(_HEADER_NAME_KEYWORD + r"$")
HEADER_NAME_OPEN = re.compile(_HEADER_NAME_KEYWORD + r"<[^>]*$")
HAS_OPERATOR = re.compile(r"\b__has_\w+")
# Refused in code (comments and literal bodies blanked).
DIGRAPH = re.compile(r"<:|:>|<%|%>|%:")
TOKEN_PASTE = re.compile(r"##")
# GCC, Clang and MSVC accept `$` in identifiers by default; the pp-number model does not, so a `$`
# in code is refused (r7, fable r6 H3: `a$1'x'` read as a digit separator hid a later call).
DOLLAR = re.compile(r"\$")
# Only the plain condition counts: a negated or compound one is "other" (fail closed). Each
# argument is a pp-number, read as a C preprocessor integer literal by _pp_int (r4, fable key3 r3).
_PP_NUMBER = r"(\.?\d(?:[eEpP][+-]|'[0-9A-Za-z_]|[0-9A-Za-z_.])*)"
VERSION_CHECK = re.compile(
    rf"QT_VERSION\s*(>=|<)\s*QT_VERSION_CHECK\s*\(\s*{_PP_NUMBER}\s*,\s*{_PP_NUMBER}\s*,\s*{_PP_NUMBER}\s*\)"
)
_INT_LITERAL = re.compile(
    r"(?:0[xX](?P<hex>[0-9A-Fa-f](?:'?[0-9A-Fa-f])*)|0[bB](?P<bin>[01](?:'?[01])*)"
    r"|(?P<oct>0(?:'?[0-7])*)|(?P<dec>[1-9](?:'?[0-9])*))"
    r"(?:[uU](?:ll|LL|[lL])?|(?:ll|LL|[lL])[uU]?)?"
)


# Tripwire (card PLAYER-TOOLBAR-QT5-GPUDISPLAY-FLIPPED-1): which files the strict scanner must read.
# A file under platform/qt or src needs the scan when
#   (1) its raw text, comments and literals included (on purpose: it fails toward scanning), has a
#       member or qualified access of flipped or flip (QT69_MEMBER_USE); or
#   (2) with CR and CRLF read as LF and every backslash-newline splice joined, the strict scanner's
#       lexer (comments and literal bodies blanked) finds a flipped or flip identifier token in code.
#       A comment, a line break or a splice between the accessor and the name, a bare name, a macro
#       alias and a using-declaration all reach it (adversary r1 A1 on 792b6c2e); or
#   (3) the lexer does not read the text, or cannot trust where its tokens and literals end, and,
#       with NULs dropped (a UTF-16 file), every splice joined
#       (`??/` and a NUL before the break included) and every universal-character-name decoded
#       (`\uXXXX`, `\UXXXXXXXX`, `\u{...}`, `\N{...}`), a flipped or flip token stands anywhere in it,
#       comments and literals included.
# Every file git can carry under the roots is read, whatever its suffix or content: tracked, and
# untracked but not ignored (adversary r1 A2 on 792b6c2e: a suffix allowlist skipped the .cc files
# MLVApp.pro compiles; pass 2 A1, A2 on 54e0a825: a doc/build-file filter was beaten by an #include
# its regex did not read). A binary file is read too: only the exact token counts, and a PNG or zip
# under the roots has none. Without a git work tree at the root, every file under the roots is read.
# NON-PROMISE: it is a tripwire for the Qt 6.9 QImage names only. A name assembled by a token paste
# (`##`) or by a macro defined in another file is not seen (the strict scanner refuses a paste in the
# scoped files); a file outside platform/qt and src, and a git-ignored one (a PR cannot carry it; a
# force-added file is tracked and read), are not read; a source charset other than UTF-8, or UTF-16
# holding ASCII, is not decoded. It is the strict scanner, not this tripwire, that judges a use
# guarded or not.
QT69_MEMBER_USE = re.compile(r"(?:\.|->|::)[ \t]*(?:flipped|flip)\b")
QT69_NAME = re.compile(r"\b(?:flipped|flip)\b")
TRIGRAPH_SPLICE = re.compile(r"\?\?/[ \t\f\v]*\n")
UCN = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|u\{([0-9A-Fa-f]+)\}|N\{([^}\n]*)\})")
TRIPWIRE_ROOTS = ("platform/qt", "src")
# Rule 3 inputs (r2, adversary r1 pass 3 A1 on e6717ff8: rule 2 lexed a text whose token ends it
# could not trust). In the raw text, and again with every splice joined: a control byte, a trigraph,
# a header-name the lexer cannot read and an include_alias token (a `%:include <a/*>` is not anchored
# at `#`, so the lexer read its `/*` as a comment). Then the lexer's own refusals (a raw string, a
# backslash in code, an unreadable header-name) and these kinds of _refused_forms, each of which moves
# where a token or a literal ends: `$` and a non-ASCII character join an identifier the lexer ends
# early, so a following ' reads as a digit separator; a digit separator is the end of the number and
# a char literal to C++11 and C99; a digraph can open a directive whose header-name the lexer reads
# as a comment or a literal. The other kinds (a token paste, macro indirection, an unknown directive,
# a redefined version macro) leave every token where the lexer put it, so they stay with rule 2: sent
# to rule 3 they would flag three files whose only flip token is in a comment.
UNLEXED_RAW_FORMS = (CONTROL_BYTE, TRIGRAPH, HEADER_NAME_UNREADABLE, INCLUDE_ALIAS)
TOKEN_BOUNDARY_KINDS = frozenset(("dollar", "non-ascii", "digit-separator", "digraph"))


def _decode_ucn(match: re.Match) -> str:
    digits = match.group(1) or match.group(2) or match.group(3)
    if digits:
        value = int(digits, 16)
        return chr(value) if value < 0x110000 else "?"
    try:
        return unicodedata.lookup(match.group(4))
    except KeyError:
        return "?"


def _reaches_qt69_name(text: str) -> bool:
    if QT69_MEMBER_USE.search(text):
        return True
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    joined = UCN.sub(_decode_ucn, TRIGRAPH_SPLICE.sub("", SPLICE.sub("", text.replace("\x00", ""))))
    if "flip" not in joined:
        return False  # no rule can fire, so only such a text is lexed
    spliced = SPLICE.sub("", text)
    if any(pattern.search(form) for form in (text, spliced) for pattern in UNLEXED_RAW_FORMS):
        return bool(QT69_NAME.search(joined))  # (3)
    lines, refused = _logical_lines(spliced)
    if refused or any(kind in TOKEN_BOUNDARY_KINDS for _, kind in _refused_forms(lines)):
        return bool(QT69_NAME.search(joined))  # (3)
    return any(QT69_NAME.search(line) for _, line, _ in lines)  # (2)


def files_needing_scan(texts: dict[str, str]) -> set[str]:
    """The keys of `texts` whose text reaches a flipped or flip name (see the tripwire above)."""
    return {key for key, text in texts.items() if _reaches_qt69_name(text)}


def _tripwire_paths(root: Path) -> list[Path]:
    """Every file git can carry under the roots (tracked, plus untracked and not ignored); every file
    under them when `root` is not the top of a git work tree."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=120)
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard", "--", *TRIPWIRE_ROOTS], capture_output=True, timeout=300)
        git_ok = (top.returncode == 0 and listed.returncode == 0
                  and Path(top.stdout.strip()).resolve() == root.resolve())
    except (OSError, subprocess.SubprocessError):
        git_ok = False
    if git_ok:
        paths = [root / name for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    else:
        paths = [root / sub for sub in TRIPWIRE_ROOTS]
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():  # the no-git fallback, or a submodule gitlink
            files.update(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files.add(path)
    return sorted(files)


def _tripwire_texts(root: Path = ROOT) -> dict[str, str]:
    return {str(path): path.read_bytes().decode("utf-8", errors="replace") for path in _tripwire_paths(root)}


def _pp_int(token: str) -> int | None:
    """The value the preprocessor gives an integer literal (leading 0 octal, 0x hex, 0b binary,
    ' digit separators, u/l/ll suffixes), or None when it is not one: an unparsable version
    argument makes the condition "other", which is never trusted."""
    literal = _INT_LITERAL.fullmatch(token)
    if not literal:
        return None
    for group, base in (("hex", 16), ("bin", 2), ("oct", 8), ("dec", 10)):
        digits = literal.group(group)
        if digits is not None:
            value = int(digits.replace("'", ""), base)
            return value if value < 2**63 else None  # past intmax_t the #if is ill-formed
    return None


def _qt_version_value(major: int, minor: int, patch: int) -> int | None:
    """QT_VERSION_CHECK(major, minor, patch) is ((major << 16) | (minor << 8) | (patch)). A value
    of 2**31 or more is None: the preprocessor's intmax_t / uintmax_t arithmetic can overflow or
    wrap there (adversary r4 A4: (2**48 + 5ULL) << 16 wraps to 0x050900), and no Qt is near it."""
    value = (major << 16) | (minor << 8) | patch
    return value if value < 2**31 else None


Line = tuple[int, str, list[int]]  # (physical start line, logical line, physical line per char)


def _physical(where: list[int], k: int, number: int) -> int:
    return where[k] if 0 <= k < len(where) else number


# Preprocessing tokens by maximal munch, enough to tell where a pp-number ends: a pp-number is a
# digit or `.digit`, then digits, identifier characters, `.`, `e+ e- E+ E- p+ p- P+ P-`, and `'`
# followed by a digit or nondigit ([lex.ppnumber]).
_PP_TOKEN = re.compile(r"(\.?[0-9](?:[eEpP][+-]|'[0-9A-Za-z_]|[\w.])*)|[^\W\d]\w*|.", re.S)
_PP_NONDIGIT_OR_DIGIT = re.compile(r"[0-9A-Za-z_]")


def _ends_in_pp_number(code: str) -> bool:
    last = None
    for last in _PP_TOKEN.finditer(code):
        pass
    return bool(last and last.group(1) and last.end() == len(code))


def _logical_lines(text: str) -> tuple[list[Line], list[tuple[int, str]]]:
    """The logical lines of a splice-free, trigraph-free, LF-only text, and the first refusal the
    lexer meets (a raw string, or a backslash in code). Comments and the bodies of string and char
    literals are blanked, so a name inside them never counts; a block comment that spans lines
    joins them into one logical line, as the preprocessor reads it; an unterminated ' or " ends at
    the line end."""
    out: list[Line] = []
    current: list[str] = []
    where: list[int] = []  # physical line of each character in `current` (adversary r4 A5)
    start = line = 1
    state = "code"  # code | line_comment | block_comment | string | char

    def emit(chars: str) -> None:
        current.extend(chars)
        where.extend([line] * len(chars))

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            if state in ("line_comment", "string", "char"):
                state = "code"
            line += 1
            if state == "block_comment":
                i += 1
                continue
            out.append((start, "".join(current), where))
            current, where, start = [], [], line
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            # Inside a `<...>` header-name, /*, //, quotes and a backslash are plain text; in a
            # "..." one a backslash is (adversary r5 pass 9 A1). The lexer would read them as a
            # comment, a literal or an escape, so the header-name is refused.
            if (ch == "/" and nxt in ("/", "*")) or ch in ("\"", "'", "\\"):
                code = "".join(current)
                if HEADER_NAME_OPEN.search(code):
                    return out, [(line, "header-name")]
                if ch == '"' and HEADER_NAME_KEYWORD.search(code):
                    close = re.compile(r'["\n\\]').search(text, i + 1)
                    if close and close.group(0) == "\\":
                        return out, [(line, "header-name")]
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                emit(" ")
                i += 2
                continue
            code = "".join(current)
            # A ' continues a pp-number only when a digit or nondigit follows it (1'000, .5'0,
            # 1e+a'0); anywhere else it opens a char literal (u8'x', 1'"'), by maximal munch.
            if ch == "'" and _PP_NONDIGIT_OR_DIGIT.match(nxt) and _ends_in_pp_number(code):
                emit(ch)
                i += 1
                continue
            if ch == '"' and re.search(r"\w*$", code).group(0).endswith("R"):
                return out, [(line, "raw-string")]  # R"d(...)d", u8R"...": not modelled
            if ch == "\\":
                return out, [(line, "backslash")]  # a universal-character-name can spell an API name
            if ch in "\"'":
                state = "string" if ch == '"' else "char"
            emit(ch)
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        elif state in ("string", "char"):
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == ('"' if state == "string" else "'"):
                state = "code"
                emit(ch)
        i += 1
    out.append((start, "".join(current), where))
    return out, []


def _refused_forms(lines: list[Line]) -> list[tuple[int, str]]:
    """(physical line, kind) of each form the scanner refuses in code (see the GRAMMAR)."""
    refused: list[tuple[int, str]] = []
    defined = {m.group(1) for _, line, _ in lines if (m := DEFINE.match(line))}
    for number, line, where in lines:
        for kind, pattern in (("digraph", DIGRAPH), ("token-paste", TOKEN_PASTE), ("non-ascii", NOT_PRINTABLE_ASCII),
                              ("dollar", DOLLAR)):
            refused.extend((_physical(where, m.start(), number), kind) for m in pattern.finditer(line))
        # r2 (card PLAYER-TOOLBAR-QT5-GPUDISPLAY-FLIPPED-1): a pp-number the lexer read with a digit
        # separator. C++11 (the Qt 5 Linux kit, MLVApp.pro:176) and C99 end the number at the ', which
        # opens a char literal there, so where the literal ends is not certain.
        refused.extend((_physical(where, token.start(1) + token.group(1).index("'"), number), "digit-separator")
                       for token in _PP_TOKEN.finditer(line) if token.group(1) and "'" in token.group(1))
        name = DIRECTIVE_NAME.match(line)
        if name and name.group(1) not in MODELLED_DIRECTIVES:
            refused.append((_physical(where, name.start(1), number), "unknown-directive"))
        if MODULE_DIRECTIVE.match(line):
            refused.append((number, "unknown-directive"))
        if VERSION_MACRO_REDEFINED.match(line):
            refused.append((number, "version-macro-redefined"))
        define = DEFINE.match(line)
        if define:
            # __has_include and its kin are operators of #if; spelled through a macro they would
            # read a header-name the scanner never sees (adversary r5 pass 8 A1 sibling).
            for name in HAS_OPERATOR.finditer(line, define.start(3)):
                refused.append((_physical(where, name.start(), number), "header-name"))
            for name in IDENTIFIER.finditer(line, define.start(3)):
                if name.group(0) in defined:
                    refused.append((_physical(where, name.start(), number), "macro-indirection"))
    return refused


def unguarded_newer_api_calls(text: str) -> list[tuple[int, str, str]]:
    """(line, api name, line text) for each API name token outside an arm that compiles only on a
    Qt new enough for it; or, when the text has any form the GRAMMAR refuses, only the refusals,
    as (line, "REFUSED <kind>", line text): such a file is unprovable and nothing else is read."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    physical_lines = text.split("\n")
    refused = [
        (text.count("\n", 0, m.start()) + 1, kind)
        for kind, pattern in (("control-byte", CONTROL_BYTE), ("splice", SPLICE), ("trigraph", TRIGRAPH),
                              ("header-name", HEADER_NAME_UNREADABLE), ("header-name", INCLUDE_ALIAS))
        for m in pattern.finditer(text)
    ]
    lines: list[Line] = []
    if not refused:
        lines, refused = _logical_lines(text)
    if not refused:
        refused = _refused_forms(lines)
    if refused:
        return sorted({(n, f"REFUSED {kind}", physical_lines[n - 1].strip()) for n, kind in refused})
    return _scan(lines)


def _scan(lines: list[Line]) -> list[tuple[int, str, str]]:
    # Each frame: [kind, version, arm_floor]. kind "ge" (#if QT_VERSION >= V), "lt" (#if QT_VERSION
    # < V) or "other"; V and arm_floor are QT_VERSION_CHECK values, arm_floor the lowest Qt the
    # current arm can compile on, or None.
    stack: list[list] = []
    found: list[tuple[int, str, str]] = []

    def trusted(api: str, floors: tuple[int, ...]) -> bool:
        needed = _qt_version_value(*NEWER_THAN_QT5_API[api], 0)
        return any(floor >= needed for floor in floors)

    for number, line, where in lines:
        directive = DIRECTIVE.match(line)
        # A name in a conditional directive is judged with no arm open (fail closed); on any
        # other line, by the arms open there. Each name is placed on its own physical line.
        floors = () if directive else tuple(frame[2] for frame in stack if frame[2] is not None)
        for match in API_NAME.finditer(line):
            if not trusted(match.group(1), floors):
                found.append((_physical(where, match.start(1), number), match.group(1), line.strip()))
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = VERSION_CHECK.fullmatch(rest.strip()) if keyword == "if" else None
                args = [_pp_int(check.group(k)) for k in (2, 3, 4)] if check else [None]
                version = None if None in args else _qt_version_value(*args)
                if check and version is not None:
                    if check.group(1) == ">=":
                        stack.append(["ge", version, version])
                    else:
                        stack.append(["lt", version, None])
                else:
                    stack.append(["other", None, None])
            elif keyword.startswith("elif") and stack:
                stack[-1] = ["other", None, None]  # an #elif / #elifdef / #elifndef arm is never trusted
            elif keyword == "else" and stack:
                kind, version, _ = stack[-1]
                stack[-1][2] = version if kind == "lt" else None
            elif keyword == "endif" and stack:
                stack.pop()
    found.sort(key=lambda hit: hit[0])
    return found


class PlayerTransportToolbarQtCompatTests(unittest.TestCase):
    def test_scoped_headers_exist(self) -> None:
        # A rename must not turn the guard below into a silent pass.
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                self.assertTrue(header.is_file(), f"scoped header missing: {header}")

    def test_scoped_headers_have_no_unguarded_newer_qt_api(self) -> None:
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                hits = unguarded_newer_api_calls(header.read_text(encoding="utf-8"))
                self.assertFalse(
                    hits,
                    f"{header.name}: an API newer than Qt 5.15 outside a matching "
                    "`#if QT_VERSION >= QT_VERSION_CHECK(...)` arm breaks the Qt 5 release kit "
                    "(macOS-Intel.yml qt@5); add a #else branch (QImage::flipped -> mirrored()). "
                    "A REFUSED finding is a form the scanner does not read (see its GRAMMAR): "
                    + "; ".join(f"line {n}: {api}: {src}" for n, api, src in hits),
                )

    def test_every_file_reaching_a_qt69_qimage_name_is_scoped(self) -> None:
        # Tripwire: an unscoped file with `.flipped`, `->flip`, `::flipped`, ... fails here, so the
        # strict scanner above is made to read it. See QT69_MEMBER_USE for what it does not promise.
        scoped = {str(path) for path in SCOPED_HEADERS}
        unscoped = sorted(files_needing_scan(_tripwire_texts()) - scoped)
        self.assertFalse(
            unscoped,
            "a file reaches QImage::flipped/flip (Qt 6.9+); add the file to SCOPED_HEADERS so the "
            "strict scanner reads it: " + "; ".join(unscoped),
        )

    def test_the_tripwire_flags_member_and_qualified_accesses_only(self) -> None:
        flagged = ("img.flipped(Qt::Vertical)", "p->flip(Qt::Horizontal)", "&QImage::flipped", "img . flipped")
        for text in flagged:
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), {"f"})
        # GpuDisplayViewport.cpp:43 on 284f00be has the first one, in a comment, and :1382 the second.
        # Since adversary r1 A1 a bare `flipped` token in code is a use, so the prose two are fed as
        # the comments they are in the tree.
        not_flagged = ("/* vertically flipped (upside down) */", "// y-flipped", "flippedCopy", "unflipped()")
        for text in not_flagged:
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), set())

    def test_the_tripwire_sees_through_comments_line_breaks_and_splices(self) -> None:
        # adversary r1 A1 (792b6c2e): a comment between the accessor and the name hid the call from
        # the raw regex. Its input, on the real GpuDisplayViewport.cpp, and every sibling form.
        viewport = (ROOT / "platform" / "qt" / "GpuDisplayViewport.cpp").read_text(encoding="utf-8")
        self.assertIn('if ( image.save(path, "PNG") )', viewport)
        a1 = viewport.replace('if ( image.save(path, "PNG") )',
                              'if ( image./*orientation*/flipped(Qt::Vertical).save(path, "PNG") )')
        self.assertEqual(files_needing_scan({"GpuDisplayViewport.cpp": a1}), {"GpuDisplayViewport.cpp"})
        for text in ("img./*c*/flipped()", "img->/* a\n b */flip(Qt::Horizontal)", "img.// c\nflipped()",
                     "img.\nflipped()", "img\n.\nflip()", "&QImage::\r\nflipped", "img.fl\\\nipped()",
                     "img.fl\\ \r\nipped()", "img./**/ /**/flipped()", "auto f = flipped;",
                     "#define F flipped\nimg.F();", "using QImage::flip;", "x(); img.flipped(); /* */"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), {"f"})
        # A name only in a comment or a string literal is not a use, as the strict scanner reads it.
        for text in ('auto s = "flipped"; // y-flipped\n', "/* img. */ x(); /* flipped */\n",
                     "auto c = 'f'; // flip\n"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), set())

    def test_an_unlexable_text_is_read_for_the_name_after_ucns_and_splices(self) -> None:
        # Where the lexer refuses (a raw string, a backslash in code, a header-name it cannot read, a
        # trigraph or a control byte), the tripwire fails toward scanning: it looks for a flipped or
        # flip token anywhere, comments and literals included, once NULs are dropped, every splice is
        # joined and every universal-character-name is decoded.
        for text in ('auto s = R"(x)"; auto f = &QImage::fl\\u0069pped;', 'auto s = R"(x)"; // flip',
                     "auto p = img.fl\\u0069pped();", "auto p = img.fl\\U00000069pped();",
                     "auto p = img.fl\\u{69}pped();", "auto p = img.fl\\N{LATIN SMALL LETTER I}pped();",
                     "??= x(); flip", "x();\x00 flipped", "f\x00l\x00i\x00p\x00p\x00e\x00d\x00",
                     "auto p = img.fl??/\nipped();", "auto p = img.fl\\\x00\nipped();"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), {"f"})
        # A refused text without the token: a Windows path is not a UCN, a binary with `flipX` is not
        # the name, and a UCN that decodes to an identifier character joins the token it touches.
        for text in ('auto s = R"(x)";', "auto c = \\n;", "??= x();", 'auto s = R"(C:\\Users\\a)"; // flippedCopy',
                     "x();\x00 flippedCopy", "\x89PNG\x00\x01flipX\x7f\\u0066lipz", "auto p = \\u00e9flipped;"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), set())

    def test_the_tripwire_does_not_lex_a_text_whose_token_ends_it_cannot_trust(self) -> None:
        # adversary r1 pass 3 A1 (e6717ff8): a `$` joins `a$1` into one identifier for the compiler,
        # but the lexer read `1'x` as a pp-number with a digit separator, and its next ' opened a char
        # literal that hid the call. Its input, on the real GpuDisplayViewport.cpp (in memory).
        viewport = (ROOT / "platform" / "qt" / "GpuDisplayViewport.cpp").read_text(encoding="utf-8")
        anchor = '    if ( image.save(path, "PNG") )'
        self.assertIn(anchor, viewport)
        a1 = viewport.replace(anchor, "#define MLV_REVIEW_SECOND(a, b) b\n    const QImage reviewImage = "
                              "MLV_REVIEW_SECOND(a$1'x', image . /*orientation*/ flipped(Qt::Vertical));\n"
                              "#undef MLV_REVIEW_SECOND\n" + anchor)
        self.assertEqual(files_needing_scan({"GpuDisplayViewport.cpp": a1}), {"GpuDisplayViewport.cpp"})
        # Every sibling form that moves where a token or a literal ends goes to rule 3: a non-ASCII
        # identifier character (U+00B7); a digit separator, which the Qt 5 Linux kit (C++11,
        # MLVApp.pro:176) and a C99 file read as the end of the number and a char literal; a digraph
        # directive whose header-name the lexer reads as a comment, also with a line-spanning comment
        # or a splice before it; and an include_alias header-name.
        call = "image . /**/ flipped(Qt::Vertical));\n"
        hidden = "auto r = image .\nflipped(Qt::Vertical); // */\n"
        for text in ("#define S(a, b) b\nauto r = S(a$1'x', " + call,
                     "#define S(a, b) b\nauto r = S(a·1'x', " + call,
                     "#define S(a, b) b\nauto r = S(1'x', " + call,
                     "%:include <QImage/*>\n" + hidden, "%: include /*\n*/ <QImage/*>\n" + hidden,
                     "%:inc\\\nlude <QImage/*>\n" + hidden, "#pragma include_alias(<a/*>, <b>)\n" + hidden,
                     "auto n = 1'000; // flipped\n"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), {"f"})
        # Proven inert, read by rule 2 or already by rule 3: `@` and a backtick are single tokens to
        # GCC, Clang and MSVC; a splice inside a literal is joined before lexing, as phase 2 does; a raw
        # string after a `$` and a UCN in a literal.
        for text in ('auto s = @"x"; auto r = ' + call, "auto s = `a`; auto r = " + call,
                     'auto s = "a\\\n"; auto r = ' + call, "auto c = 'a\\\n'; auto r = " + call,
                     'auto s = a$R"(x)"; auto r = ' + call, 'auto s = "\\u0022"; auto r = ' + call):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), {"f"})
        # A text with none of the names is still not flagged, whatever it holds.
        for text in ("auto n = 1'000; auto s = a$b; %:include <a/*>\n", "auto r = a·b; // mirrored\n"):
            with self.subTest(text=text):
                self.assertEqual(files_needing_scan({"f": text}), set())

    def test_a_digit_separator_in_code_is_refused(self) -> None:
        # r2 sibling of adversary r1 pass 3 A1: the Qt 5 Linux kit compiles C++11 (MLVApp.pro:176),
        # where `1'x'` is the number 1 and a char literal, not a pp-number; read as C++14 the next '
        # opened a char literal that hid the call. A digit separator in code is refused.
        self.assertRefused("#define S(a, b) b\nreturn S(1'x', image.flipped(Qt::Horizontal));\n",
                           ["digit-separator"], [2])
        for source in ("const int n = 1'000;\n", "auto d = .5'0;\n", "auto h = 0x1'f;\n",
                       "#if QT_VERSION >= QT_VERSION_CHECK(6, 1'0, 0)\n#endif\n"):
            with self.subTest(source=source):
                self.assertRefused(source, ["digit-separator"], [1])
        # A ' after a number that no digit or nondigit follows is a char literal, and in a comment or a
        # literal a separator is text.
        self.assertEqual(unguarded_newer_api_calls("auto c = f(1,'x'); // 1'000\nauto s = \"2'0\";\n"), [])

    def test_the_tripwire_reads_every_file_under_the_roots(self) -> None:
        # adversary r1 A2 (792b6c2e): a suffix allowlist skipped the .cc files MLVApp.pro compiles
        # (MLVApp.pro:301-314). adversary r1 pass 2 A1, A2 (54e0a825): the doc/build-file filter that
        # replaced it was beaten by an #include its regex did not read, and by a transitive one.
        # Every file git can carry under the roots is read now, whatever its suffix or content.
        texts = _tripwire_texts()
        ahd = ROOT / "src" / "librtprocess" / "src" / "demosaic" / "ahd.cc"
        self.assertTrue(str(ahd) in texts, f"the tripwire does not read {ahd}")
        injected = dict(texts)
        injected[str(ahd)] = ("#include <QImage>\nstatic QImage qt69Readback(const QImage &image) "
                              "{ return image.flipped(Qt::Vertical); }\n" + texts[str(ahd)])
        self.assertIn(str(ahd), files_needing_scan(injected))
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *TRIPWIRE_ROOTS],
                                 capture_output=True, check=True).stdout.decode("utf-8").split("\0")
        unread = sorted(name for name in tracked if name and (ROOT / name).is_file() and str(ROOT / name) not in texts)
        self.assertEqual(len(unread), 0, f"the tripwire does not read tracked files: {unread[:20]}")
        # The pass 2 inputs, in a tree that is not a git work tree (every file under the roots).
        payload = (b"#include <QImage>\nstatic QImage qt69Readback(const QImage &image) "
                   b"{ return image.flipped(Qt::Vertical); }\n")
        files = {
            "platform/qt/GpuDisplayViewport.cpp":
                b'#include /*orientation*/ "qt69-readback.txt"\n#include "qt69-outer.md"\nx();\n',
            "platform/qt/qt69-readback.txt": payload,
            "platform/qt/qt69-outer.md": b'#include "qt69-inner.txt"\n',
            "platform/qt/qt69-inner.txt": payload,
            "platform/qt/Makefile.qt69.cpp": payload,
            "platform/qt/Makefile": b"GpuDisplayWindow.o: C:\\Users\\x\\flipper.cpp\n",
            "src/k/nul.txt": b"x();\x00 auto f = flipped;\n",
            "src/k/icon.png": b"\x89PNG\x00\x01flipX\x00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(data)
            texts = _tripwire_texts(root)
            self.assertEqual(sorted(texts), sorted(str(root / name) for name in files))
            expected = ("platform/qt/qt69-readback.txt", "platform/qt/qt69-inner.txt",
                        "platform/qt/Makefile.qt69.cpp", "src/k/nul.txt")
            self.assertEqual(files_needing_scan(texts), {str(root / name) for name in expected})

    def test_scanner_flags_the_09077b0d_shape(self) -> None:
        # r1's own line: proves the guard bites on the defect it exists for.
        fixture = (
            "inline QIcon mirroredIcon( const QString &resource )\n"
            "{\n"
            "    const QImage image( resource );\n"
            "    return QIcon( QPixmap::fromImage( image.flipped( Qt::Horizontal ) ) );\n"
            "}\n"
        )
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(fixture)], [(4, "flipped")])

    def test_scanner_flags_each_api_and_member_form(self) -> None:
        for call in ("img.flipped()", "img->flipped( Qt::Vertical )", "img.flip()", "img.QImage::flipped()",
                     "&QImage::flipped", "( img . flipped )()", "e->position()", "e.globalPosition()",
                     "e.scenePosition()"):
            with self.subTest(call=call):
                self.assertEqual(len(unguarded_newer_api_calls(f"auto p = {call};\n")), 1)

    def test_scanner_accepts_the_guarded_shapes(self) -> None:
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#else\n"
            "    return image.mirrored( true, false );\n"
            "#endif\n"
        )
        lt_form = (
            "#if QT_VERSION < QT_VERSION_CHECK(6, 9, 0)\n"
            "    return image.mirrored( true, false );\n"
            "#else\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#endif\n"
        )
        newer = "#if QT_VERSION >= QT_VERSION_CHECK(7, 0, 0)\nreturn image.flipped();\n#endif\n"
        self.assertEqual(unguarded_newer_api_calls(ge_form), [])
        self.assertEqual(unguarded_newer_api_calls(lt_form), [])
        self.assertEqual(unguarded_newer_api_calls(newer), [])

    def test_a_qt6_0_branch_is_not_enough_for_a_qt6_9_api(self) -> None:
        # flipped() needs 6.9: a Qt 6.0 arm still breaks Qt 6.0-6.8, but is enough for position().
        fixture = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "return image.flipped();\n"
            "auto p = e->position();\n"
            "#endif\n"
        )
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(fixture)], [(2, "flipped")])

    def test_scanner_flags_a_call_in_the_old_arm_or_after_the_branch(self) -> None:
        fixture = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "    return image.mirrored( true, false );\n"
            "#else\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#endif\n"
            "auto q = image.flipped();\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(fixture)], [4, 6])

    def test_scanner_only_trusts_the_plain_version_condition(self) -> None:
        for condition in (
            "!(QT_VERSION >= QT_VERSION_CHECK(6, 9, 0))",
            "QT_VERSION >= QT_VERSION_CHECK(6, 9, 0) || 1",
            "QT_VERSION < QT_VERSION_CHECK(6, 9, 0) && 0",
        ):
            with self.subTest(condition=condition):
                fixture = f"#if {condition}\nreturn i.flipped();\n#else\nreturn i.flipped();\n#endif\n"
                self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(fixture)], [2, 4])
        elif_form = (
            "#if QT_VERSION < QT_VERSION_CHECK(5, 0, 0)\nx();\n"
            "#elif QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\nreturn i.flipped();\n#endif\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(elif_form)], [4])

    def test_scanner_ignores_comments_and_literals(self) -> None:
        fixture = (
            "// image.flipped() is Qt 6.9 only\n"
            "/* img.flip() and\n"
            "   e->position() */\n"
            'const char *s = "img.flipped()";\n'
            "const char c = 'f', *t = \"e->position() \\\" img.flip()\";\n"
            "auto p = image.mirrored( true, false );\n"
        )
        self.assertEqual(unguarded_newer_api_calls(fixture), [])
        self.assertEqual(len(unguarded_newer_api_calls('const char *s = "//"; auto p = i.flipped();\n')), 1)
        # r2: a digit separator is refused (test_a_digit_separator_in_code_is_refused).
        self.assertRefused("const int n = 1'000; auto p = i.flipped();\n", ["digit-separator"], [1])

    def assertRefused(self, source: str, kinds: list[str], lines: list[int] | None = None) -> None:
        # r5 GRAMMAR: a refused form makes the whole file unprovable; only refusals are reported.
        hits = unguarded_newer_api_calls(source)
        self.assertEqual(sorted({api for _, api, _ in hits}), sorted(f"REFUSED {k}" for k in set(kinds)), hits)
        if lines is not None:
            self.assertEqual(sorted({n for n, _, _ in hits}), lines, hits)

    def test_digraphs_are_refused(self) -> None:
        # r5: digraphs are not modelled (r2 read `%:` as `#`), so a digraph in code is refused.
        fixture = (
            "%: if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "return i.flipped();\n"
            "%:else\n"
            "return i.flipped();\n"
            "%:endif\n"
        )
        self.assertRefused(fixture, ["digraph"], [1, 3, 5])
        for source in ("int a<:1:>;\n", "void f() <% %>\n", "x = a <::b;\n"):
            with self.subTest(source=source):
                self.assertRefused(source, ["digraph"], [1])
        # In a comment or a literal a digraph is text.
        self.assertEqual(unguarded_newer_api_calls('// <: %:\nauto s = ":/a<%";\n'), [])

    def test_scanner_sees_macro_aliases_and_refuses_trigraphs(self) -> None:
        # adversary r2 A2 (da60b814): both inputs used to return no finding.
        alias = "#define FLIP flipped\nreturn image.FLIP(Qt::Horizontal);\n"
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(alias)], [(1, "flipped")])
        trigraph = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n"
            "??=endif\n"
            "return image.flipped(Qt::Horizontal);\n"
        )
        self.assertRefused(trigraph, ["trigraph"], [2])
        guarded_alias = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "#define FLIP( i ) ( i ).flipped( Qt::Horizontal )\n"
            "#else\n"
            "#define FLIP( i ) ( i ).mirrored( true, false )\n"
            "#endif\n"
            "#define MIRROR mirrored\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded_alias), [])

    def test_scanner_sees_a_line_break_inside_the_member_access(self) -> None:
        # r3 keys on fcbae668 (sol B1, opus, fable key3): each input used to return no finding,
        # because the matcher ran per logical line. Whitespace, including a line break, CR or CRLF,
        # between the accessor and the name, or between QImage and ::, is inside the grammar.
        for source, api in (
            ("return image.\nflipped(Qt::Horizontal);\n", "flipped"),
            ("return image->\nflipped(Qt::Horizontal);\n", "flipped"),
            ("auto f = &QImage::\nflipped;\n", "flipped"),
            ("return image.QImage::\nflipped(Qt::Horizontal);\n", "flipped"),
            ("auto p = event->\nposition();\n", "position"),
            ("return image.\r\nflipped(Qt::Horizontal);\r\n", "flipped"),
            ("auto f = &QImage ::\nflipped;\n", "flipped"),
            ("auto f = &QImage\n::flipped;\n", "flipped"),
            ("return image\n.\nflipped();\n", "flipped"),
            ("return image. /* c */\n// line\nflipped();\n", "flipped"),
            ("return image.\r\r\nflipped();\n", "flipped"),
        ):
            with self.subTest(source=source):
                self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(source)], [api])
        # The finding names the line the API name is on.
        self.assertEqual(
            [(n, a) for n, a, _ in unguarded_newer_api_calls("x();\nreturn image.\nflipped();\n")],
            [(3, "flipped")],
        )

    def test_a_line_break_member_access_is_judged_at_the_name(self) -> None:
        # The name token is what needs the newer Qt: inside the arm it is guarded, outside it is not.
        guarded = (
            "return image.\n"
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "flipped( Qt::Horizontal );\n"
            "#else\n"
            "mirrored( true, false );\n"
            "#endif\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded), [])
        unguarded = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "return image.\n"
            "#endif\n"
            "flipped( Qt::Horizontal );\n"
        )
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(unguarded)], [(4, "flipped")])

    def test_version_arguments_are_read_as_preprocessor_integer_literals(self) -> None:
        # fable key3 r3: 010 is octal 8, so this arm also compiles on Qt 6.8 and is not a guard.
        def arm(major: str, minor: str, patch: str = "0", op: str = ">=") -> str:
            body = "return image.flipped();\n"
            other = "return image.mirrored( true, false );\n"
            head = f"#if QT_VERSION {op} QT_VERSION_CHECK( {major}, {minor}, {patch} )\n"
            return head + (body + "#else\n" + other if op == ">=" else other + "#else\n" + body) + "#endif\n"

        flagged = (
            ("6", "010", "0"),  # octal 8
            ("6", "0x8", "0"),  # hex 8
            ("6", "0b1000", "0"),  # binary 8
            ("6", "08", "0"),  # not a valid octal literal: unparsable is never a pass
            ("6", "9.0", "0"),  # not an integer literal
            ("6", "9z", "0"),  # not a preprocessor integer suffix
            ("6", "9", "0x"),  # a malformed patch is unparsable too
            ("6", "8", "255"),  # 6.8.255 is still below 6.9
        )
        for args in flagged:
            for op in (">=", "<"):
                with self.subTest(args=args, op=op):
                    self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(arm(*args, op=op))], ["flipped"])
        accepted = (
            ("6", "011", "0"),  # octal 9
            ("6", "0x9", "0"),
            ("6", "0X0A", "0"),
            ("6", "0b1001", "0"),
            ("6u", "9U", "0ul"),
            ("6LL", "9llu", "0"),
            ("06", "9", "0"),
            ("6", "8", "256"),  # QT_VERSION_CHECK is (6<<16)|(8<<8)|256 == 6.9.0
            ("7", "0", "0"),
        )
        for args in accepted:
            for op in (">=", "<"):
                with self.subTest(args=args, op=op):
                    self.assertEqual(unguarded_newer_api_calls(arm(*args, op=op)), [])

    def test_an_apostrophe_in_directive_text_does_not_hide_later_code(self) -> None:
        # An unterminated ' or " ends at the line end, as the preprocessor reads it.
        for directive in ("#error it's Qt 6 only", '#warning "unterminated', "#pragma message 'x"):
            with self.subTest(directive=directive):
                fixture = f"{directive}\nreturn image.flipped();\n"
                self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(fixture)], [(2, "flipped")])

    def test_a_trigraph_is_refused_anywhere(self) -> None:
        # adversary r4 A1 (8a66187d) read `??/` both ways; r5 refuses every trigraph instead, in
        # code, a directive, a comment or a literal.
        for source, line in (
            ("#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n#en??/\ndif\nreturn image.flipped();\n", 2),
            ("// note ??/\nreturn image.flipped();\n", 1),
            ('auto s = "what??!";\n', 1),
            ("x();\nint a ??( 1 ??);\n", 2),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["trigraph"], [line])
        # One call is one finding.
        self.assertEqual(len(unguarded_newer_api_calls("auto p = i.flipped(); auto q = i.flipped();\n")), 2)

    def test_every_backslash_newline_splice_is_refused(self) -> None:
        # adversary r4 pass 5 A1 (202596e2) and pass 6 A1 (3c82e35e): r5 refuses every splice, with
        # or without spaces or tabs before the line break, wherever it stands.
        for source, line in (
            ("#define JOIN(a,b) a #\\ \n# b\nreturn image.JOIN(flipp,ed)(Qt::Horizontal);\n", 1),
            ("#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n#en\\\t \ndif\nreturn image.flipped();\n", 2),
            ("// note \\  \nreturn image.flipped();\n", 1),
            ("auto p = img.flip\\\nped();\n", 1),
            ("/\\\n* x */ auto p = i.flipped();\n", 1),
            ("/* x *\\\n/ auto p = i.flipped();\n", 1),
            ("/* x *\\\r\n/ auto p = i.flipped();\r\n", 1),
            ("x();\n/* x *\\\r/ auto p = i.flipped();\r", 2),
            ('auto s = "a\\\nb";\n', 1),
            ("#define F(x) \\\n  x\n", 1),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["splice"], [line])
        # A backslash that does not end its line is not a splice (an escape in a literal).
        self.assertEqual(unguarded_newer_api_calls('auto s = "a\\\\"; auto c = \'\\n\';\n'), [])

    def test_a_backslash_in_code_is_refused(self) -> None:
        # r5: a universal-character-name can spell an API name; any backslash in code is refused.
        self.assertRefused("auto p = img.fl\\u0069pped();\n", ["backslash"], [1])
        self.assertRefused("x();\nauto p = \\U00000066lip;\n", ["backslash"], [2])

    def test_a_digit_separator_does_not_open_a_char_literal(self) -> None:
        # r5 sibling sweep: a pp-number may start with `.digit`, so `.5'0` is one number and the '
        # is a separator; read as a char literal it hid the rest of the line.
        # ...and a ' that a digit or nondigit does not follow opens a char literal even after a
        # pp-number, so the literal's `"` must not open a string (all inside a discarded macro arg).
        for source in ("auto d = .5'0; return image.flipped();\n", "auto d = x + 0x1'f; auto p = i.flip();\n",
                       "auto d = 1e+5'0; auto p = e->position();\n", "F(1e+a'0, image.flipped());\n",
                       "F(e+5'0, image.flipped());\n", "F(1'\"', image.flipped());\n",
                       "F(1'é\"', image.flipped());\n"):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)
        # r2: each separator above is now refused as such, and `1'"'` / `1'é"'` are char literals.
        for source, kind in (("auto d = .5'0; return image.flipped();\n", "REFUSED digit-separator"),
                             ("F(1'\"', image.flipped());\n", "flipped"), ("F(1'é\"', image.flipped());\n", "flipped")):
            with self.subTest(source=source):
                self.assertEqual([api for _, api, _ in unguarded_newer_api_calls(source)], [kind])
        # A char literal after an identifier with a digit (u8'x') is still a literal.
        self.assertEqual(unguarded_newer_api_calls("auto c = u8'f'; auto d = U'\"'; x();\n"), [])
        self.assertEqual(len(unguarded_newer_api_calls("auto c = u8'\"'; auto p = i.flipped();\n")), 1)

    def test_an_api_name_passed_to_a_function_like_macro_is_a_finding(self) -> None:
        # adversary r4 pass 5 A2: the scan does not expand macros, so an API name in the arguments
        # of a function-like macro this text defines counts where the macro is used.
        for source in (
            "#define APPLY(i, member) (i).member(Qt::Horizontal)\nreturn APPLY(image, flipped);\n",
            "#define CALL( f ) image.f()\nauto p = CALL(\n  position );\n",
            "#define WRAP(x) x\nreturn WRAP( g( image.mirrored(), flip ) );\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)
                self.assertGreaterEqual(unguarded_newer_api_calls(source)[0][0], 2)
        guarded = (
            "#define APPLY(i, member) (i).member(Qt::Horizontal)\n"
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\nreturn APPLY(image, flipped);\n#endif\n"
            "return APPLY(image, mirrored);\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded), [])

    def test_any_qualified_api_name_is_a_finding(self) -> None:
        # adversary r4 pass 5 A3: an alias (`using Image = QImage;`) hides the class name, so any
        # `X::name` counts, not only `QImage::name`.
        for source in (
            "using Image = QImage;\nauto fn = &Image::flipped;\n",
            "typedef QImage I;\nauto fn = &I :: flip;\n",
            "auto fn = &decltype(image)::flipped;\n",
            "auto fn = &std::remove_cvref_t<decltype(image)>::flipped;\n",
            "auto p = &QMouseEvent::position;\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)

    def test_a_raw_string_is_refused(self) -> None:
        # adversary r4 pass 5 A4 found a d-char (`"`) the r4 lexer did not model; r5 refuses every
        # raw string literal, whatever its prefix and delimiter.
        for source in ('auto s = R""(//)""; return image.flipped(Qt::Horizontal);\n',
                       'auto s = R"a"b(/* )a"b"; return image.flipped();\n',
                       'auto s = R"(img.flipped())";\n', 'auto s = u8R"x()x";\n', 'auto s = LR"(a)";\n'):
            with self.subTest(source=source):
                self.assertRefused(source, ["raw-string"], [1])
        # An ordinary string or a prefix that is not raw is read.
        self.assertEqual(unguarded_newer_api_calls('auto s = u8"(a)"; auto t = L"R";\n'), [])

    def test_a_token_paste_is_refused(self) -> None:
        # adversary r4 A2: the scan does not expand macros, so it refuses a paste instead of
        # following it, wherever it sits.
        self.assertRefused("#define JOIN(a,b) a ## b\nreturn image.JOIN(flip,ped)(Qt::Horizontal);\n",
                           ["token-paste"], [1])
        self.assertRefused("#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n#define JOIN(a,b) a##b\n#endif\n",
                           ["token-paste"], [2])
        self.assertRefused("#define JOIN(a,b) a%:%:b\n", ["digraph"], [1])
        self.assertRefused("#define JOIN(a,b) a ??=??= b\n", ["trigraph"], [1])
        # `#` stringizing and a `##` in a comment or string are not pastes.
        self.assertEqual(unguarded_newer_api_calls('#define S(x) #x /* a ## b */ "##"\n'), [])

    def test_macro_indirection_is_refused(self) -> None:
        # adversary r4 pass 6 A2: a #define whose replacement names a macro this file #defines
        # (before or after it, object-like or function-like) is refused.
        for source, lines in (
            ("#define INDIRECT APPLY\n#define APPLY(i, m) (i).m()\nreturn INDIRECT(image, mirrored);\n", [1]),
            ("#define A B\n#define B C\n#define C(x) x\n", [1, 2]),
            ("#define SELF SELF\n", [1]),
            ("#define F(x) G(x)\n#define G(x) x\n", [1]),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["macro-indirection"], lines)
        # A replacement that names only parameters, keywords or names from other files is read.
        self.assertEqual(unguarded_newer_api_calls("#define F(x, m) (x).m(Qt::Horizontal)\n#define G Q_UNUSED\n"), [])

    def test_a_redefined_version_macro_is_refused(self) -> None:
        # adversary r4 A3: the guard trusts Qt's own QT_VERSION / QT_VERSION_CHECK, so r5 refuses a
        # #define or #undef of either in the scanned text.
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#else\n"
            "    return image.mirrored( true, false );\n"
            "#endif\n"
        )
        for prefix, lines in (
            ("#undef QT_VERSION_CHECK\n#define QT_VERSION_CHECK(a,b,c) 0\n", [1, 2]),
            ("#define QT_VERSION_CHECK(a, b, c) 0\n", [1]),
            ("#undef QT_VERSION\n", [1]),
            ("#define QT_VERSION 0x070000\n", [1]),
            ("  #  define QT_VERSION 0x070000\n", [1]),
        ):
            with self.subTest(prefix=prefix):
                self.assertRefused(prefix + ge_form, ["version-macro-redefined"], lines)
        self.assertRefused("%:  define QT_VERSION 0x070000\n" + ge_form, ["digraph"], [1])
        # A name that only starts with the macro name is not a redefinition.
        self.assertEqual(unguarded_newer_api_calls("#define QT_VERSION_CHECKED 1\n" + ge_form), [])

    def test_a_version_past_the_preprocessor_range_is_never_trusted(self) -> None:
        # adversary r4 A4: (2**48 + 5ULL) << 16 wraps to 0x050900 in 64-bit preprocessing.
        for major in ("281474976710661ULL", "32768", "0x8000"):
            for op in (">=", "<"):
                with self.subTest(major=major, op=op):
                    body, other = "return image.flipped();\n", "return image.mirrored( true, false );\n"
                    arms = body + "#else\n" + other if op == ">=" else other + "#else\n" + body
                    fixture = f"#if QT_VERSION {op} QT_VERSION_CHECK({major},9,0)\n{arms}#endif\n"
                    self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(fixture)], ["flipped"])

    def test_a_finding_names_the_physical_line_of_the_name(self) -> None:
        # adversary r4 A5: a multi-line comment before the call on the same logical line.
        for source in (
            "/* x\n */ return image.flipped();\n",
            "x(); /* a\n\nb */ return image.flipped();\n",
            "auto a = 1; /* one\n two */ auto p = e->\nposition();\n",
        ):
            with self.subTest(source=source):
                expected = source.rstrip("\n").count("\n") + 1
                self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(source)], [expected])
        alias = "#define A /* x\n */ flipped\n"
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(alias)], [(2, "flipped")])

    def _header_with_branch(self, branch: str) -> str:
        # The real header, its mirroredImage() version branch replaced by `branch` (in memory).
        text = SCOPED_HEADERS[0].read_text(encoding="utf-8")
        start = text.index("#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )")
        end = text.index("#endif", start) + len("#endif")
        return text[:start] + branch.rstrip("\n") + text[end:]

    def test_a_splice_inside_a_comment_delimiter_is_refused(self) -> None:
        # adversary r4 pass 6 A1 (3c82e35e): a splice inside `/*` or `*/` used to hide the call.
        for branch in ('/\\\n* " */ return image.flipped(Qt::Horizontal);\n',
                       "/* x *\\\n/ return image.flipped(Qt::Horizontal);\n"):
            with self.subTest(branch=branch):
                hits = unguarded_newer_api_calls(self._header_with_branch(branch))
                self.assertTrue(hits)
                self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)

    def test_an_object_like_alias_of_a_function_like_macro_is_refused(self) -> None:
        # adversary r4 pass 6 A2 (3c82e35e): INDIRECT expands to APPLY, whose arguments escaped.
        branch = ("#define APPLY(i, member) (i).member(Qt::Horizontal)\n#define INDIRECT APPLY\n"
                  "return INDIRECT(image, flipped);\n")
        for source in (branch, self._header_with_branch(branch)):
            with self.subTest(source=source[:40]):
                hits = unguarded_newer_api_calls(source)
                self.assertTrue(hits)
                self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)

    def test_a_character_outside_printable_ascii_in_code_is_refused(self) -> None:
        # adversary r5 A1 (4d52622e): a BOM before `#define QT_VERSION_CHECK` hid the anchored
        # directive, and the later arm was trusted. Every directive pattern is anchored at the line
        # start, so r5 refuses any character in code outside printable ASCII, tab, VT and FF.
        bom = "﻿#define QT_VERSION_CHECK(a,b,c) 0\n#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n" \
              "return image.flipped(Qt::Horizontal);\n#endif\n"
        self.assertRefused(bom, ["non-ascii"], [1])
        hits = unguarded_newer_api_calls("﻿#define QT_VERSION_CHECK(a,b,c) 0\n"
                                         + SCOPED_HEADERS[0].read_text(encoding="utf-8"))
        self.assertTrue(hits)
        self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)
        for prefix in (" ", " ", "　", "\x1c", "\x00", "​"):
            with self.subTest(prefix=repr(prefix)):
                # r7: a C0 control byte (\x1c, \x00) is refused earlier, in the raw text, as
                # control-byte (test_a_control_byte_anywhere_is_refused); it is still never read.
                kind = "control-byte" if prefix < " " else "non-ascii"
                self.assertRefused(f"x();\n{prefix}#undef Y\n", [kind], [2])
        # In a comment or a literal it is text.
        self.assertEqual(unguarded_newer_api_calls('// ﻿ café\nauto s = "é";\n'), [])

    def test_a_directive_the_scanner_does_not_model_is_refused(self) -> None:
        # adversary r5 A2 (4d52622e): `#elifdef` (C++23) was not recognised, so its arm kept the
        # trusted floor of the `#if` above it. r5 reads `#elifdef`/`#elifndef` as an untrusted
        # `#elif`, and refuses any directive name it does not model.
        for api in NEWER_THAN_QT5_API:
            for keyword in ("elifdef", "elifndef"):
                with self.subTest(api=api, keyword=keyword):
                    source = (f"#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\nreturn image.mirrored(true,false);\n"
                              f"#{keyword} __cplusplus\nreturn image.{api}(Qt::Horizontal);\n#endif\n")
                    self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(source)], [(4, api)])
        for directive in ("#include_next <QImage>", "#import <x>", "#ident \"x\"", "#assert machine(x)",
                          "#embed \"x\"", "# 12 \"file.h\"", "#sccs \"x\"", "  # /* c */ unassert x",
                          "import \"qt_override.h\";", "export import <QImage>;", "module;", "export module m;"):
            with self.subTest(directive=directive):
                self.assertRefused(f"x();\n{directive}\n", ["unknown-directive"], [2])
        # The directives the scanner models, and the null directive, are read.
        self.assertEqual(unguarded_newer_api_calls(
            "#pragma once\n#include <QImage>\n#\n#undef X\n#line 10\n#error x\n#warning x\n"), [])

    def test_a_header_name_the_lexer_cannot_read_is_refused(self) -> None:
        # adversary r5 pass 8 A1 (8354641b): in `<a/*>` after __has_include or #include the `/*` is
        # header-name text, not a comment; read as one it hid the call. r5 refuses a <...> header-name
        # holding /*, //, a quote or a backslash, a "..." one holding a backslash, and __has_include
        # spelled through a #define.
        branch = "#if __has_include(<a/*>)\n#endif\n    return image.flipped( Qt::Horizontal ); /* */\n"
        hits = unguarded_newer_api_calls(self._header_with_branch(branch))
        self.assertTrue(hits)
        self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)
        for source in (branch, "#include <a//b>\n", "#include /* c */ <a'b>\n", '#include <a"b>\n',
                       "#include <a\\b>\n", '#include "a\\" \n', "#include_next <a/*>\n",
                       "#if __has_include_next(<x//>)\n#endif\n", "#pragma GCC dependency <a/*>\n",
                       "#define HI __has_include\n"):
            with self.subTest(source=source):
                self.assertRefused(source, ["header-name"], [1])
        # A plain header-name, also with a trailing comment, is read.
        self.assertEqual(unguarded_newer_api_calls(
            '#include <QImage> // x\n#include "Player.h" /* y */\n#if __has_include(<QImage>)\n#endif\n'), [])

    def test_a_header_name_after_a_line_spanning_comment_is_refused(self) -> None:
        # adversary r5 pass 9 A1 (c804f5b6): a comment that spans lines between __has_include and its
        # `(<a/*>)` hid the header-name from the raw-text regex, and the lexer read its `/*` as a
        # comment. r6 checks the header-name context in the lexer, on the comment-blanked line.
        pass9 = "#if __has_include /*\n*/ (<a/*>)\n#endif\nreturn image.flipped(Qt::Horizontal); /* */\n"
        for source, line in (
            (pass9, 2),
            (pass9.replace("\n", "\r\n"), 2),
            (pass9.replace("\n", "\r"), 2),
            ("#if __has_include( /*\n*/ <a//b>)\n#endif\nreturn image.flipped();\n", 2),
            ("# /*\n*/ include <a'b>\nreturn image.flipped();\n", 2),
            ("#include /*\n*/ <a\"b>\nreturn image.flipped();\n", 2),
            ("#include /*\n*/ <a\\b>\nreturn image.flipped();\n", 2),
            ("#pragma GCC dependency /*\n*/ <a/*>\nreturn image.flipped(); /* */\n", 2),
            ("#include /*\n*/ \"a\\\" \" /*\nreturn image.flipped(); /* */\n", 2),
            ("#if __has_include /*\n*/ (\"a\\\" \" /*)\n#endif\nreturn image.flipped(); /* */\n", 2),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["header-name"], [line])
        for api in NEWER_THAN_QT5_API:
            with self.subTest(api=api):
                self.assertRefused(pass9.replace("flipped", api), ["header-name"], [2])
        hits = unguarded_newer_api_calls(self._header_with_branch(pass9))
        self.assertTrue(hits)
        self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)
        # A readable header-name, with comments around it, is still read.
        self.assertEqual(unguarded_newer_api_calls(
            "#if __has_include /*\n*/ (<QImage>) // y\n#endif\n#include /* a\n b */ \"Player.h\" /* c */\n"), [])

    def test_a_control_byte_anywhere_is_refused(self) -> None:
        # sol r6 B1 (c9119076): GCC reads a NUL between a backslash and the line break as
        # whitespace, so the splice closed a comment or literal the scanner kept open, and the call
        # after it was live while the scan returned []. r7 does not model that: any C0 control byte
        # but tab, LF, CR, VT and FF (and DEL) anywhere in the raw text is refused before lexing.
        nul = "\\\x00"
        for template in (
            "/* x *" + nul + "\n/ return image.{api}(Qt::Horizontal); /* */\n",
            'const char *s = "a' + nul + '\n"; return image.{api}(Qt::Horizontal);\n',
            "auto c = 'a" + nul + "\n'; return image.{api}(Qt::Horizontal);\n",
        ):
            for api in NEWER_THAN_QT5_API:
                for eol in ("\n", "\r\n", "\r"):
                    source = template.format(api=api).replace("\n", eol)
                    with self.subTest(source=source):
                        self.assertRefused(source, ["control-byte"], [1])
            branch = template.format(api="flipped")
            with self.subTest(header_branch=branch):
                hits = unguarded_newer_api_calls(self._header_with_branch(branch))
                self.assertTrue(hits)
                self.assertTrue(all(api == "REFUSED control-byte" for _, api, _ in hits), hits)
        # Every other C0 byte and DEL is refused too, in a comment, a literal or code.
        for byte in [chr(c) for c in range(0x20) if chr(c) not in "\t\n\r\v\f"] + ["\x7f"]:
            for source in (f"x();\n// a{byte}b\n", f'x();\nauto s = "{byte}";\n', f"x();\n{byte}#undef Y\n"):
                with self.subTest(byte=repr(byte), source=source):
                    self.assertRefused(source, ["control-byte"], [2])
        # Tab, VT and FF are read, in code and in a comment.
        self.assertEqual(unguarded_newer_api_calls("\tx();\v\f// a\tb\v\f\n"), [])

    def test_include_alias_is_refused(self) -> None:
        # fable r6 H2: `#pragma include_alias` reads header-names the lexer does not know; with a
        # comment spanning lines before its `<a/*>`, the `/*` hid a later call. r7 refuses the token.
        source = "#pragma include_alias( /*\n*/ <a/*>, <b>)\nreturn image.flipped(Qt::Horizontal); /* */\n"
        for text in (source, source.replace("\n", "\r\n"), '#pragma include_alias("a.h", "b.h")\n'):
            with self.subTest(text=text):
                self.assertRefused(text, ["header-name"], [1])
        hits = unguarded_newer_api_calls(self._header_with_branch(source))
        self.assertTrue(hits)
        self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)

    def test_a_dollar_in_code_is_refused(self) -> None:
        # fable r6 H3: compilers accept `$` in identifiers, so `a$1'x'` is an identifier and a char
        # literal to them, but the scanner read `1'x` as a pp-number and its next ' opened a char
        # literal that hid the call. r7 refuses a `$` in code; in a comment or a literal it is text.
        source = "#define SECOND(a, b) b\nreturn SECOND(a$1'x', image.flipped(Qt::Horizontal));\n"
        self.assertRefused(source, ["dollar", "digit-separator"], [2])  # r2: `1'x` is refused too
        self.assertRefused("auto a$b = 1;\n", ["dollar"], [1])
        self.assertEqual(unguarded_newer_api_calls('// $x\nauto s = "$"; auto c = \'$\';\n'), [])

    def test_every_api_name_token_is_a_use(self) -> None:
        # r5: the token rule is fail closed: a bare call, a macro argument (of any macro, defined
        # here or not) or a conditional directive that names an API is a use too.
        for source in ("auto b = flipped();\n", "Q_UNUSED( flip );\n", "EXTERNAL(image, flipped);\n",
                       "#if defined(position)\n#endif\n", "#error flipped is Qt 6.9\n"):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)

    def test_names_that_only_contain_an_api_name_do_not_count(self) -> None:
        fixture = "auto a = img.flippedCopy(); auto b = unflipped(); auto c = s.positions(); auto d = flip_x;\n"
        self.assertEqual(unguarded_newer_api_calls(fixture), [])


if __name__ == "__main__":
    unittest.main()
