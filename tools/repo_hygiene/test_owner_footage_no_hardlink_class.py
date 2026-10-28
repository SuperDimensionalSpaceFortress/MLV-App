"""OWNER-FOOTAGE-NO-HARDLINK-1: no tracked tool creates a hard link, and nothing that handles owner
footage can truncate or write through a name of it.

THE CLASS. A hard link is a second, equal NAME of a file's bytes. Any tool that deletes, sweeps or
truncates a path it believes is job scratch -- a recursive scratch delete, a partial-file cleanup, a
lane scratch sweep, a write through a leftover link -- can therefore destroy the owner's footage
through it. PR #200 and PR #203 each narrowed a check ("delete the link only while the link count is
at least two") and four review rounds kept finding the same class in a new place, because the
mechanism -- a hard link to owner footage -- was still there to be found.

THE RULING (hub, 2026-09-30). NO CODE PATH MAY CREATE A HARD LINK TO OWNER FOOTAGE. The app still gets
one job-private directory of neutral names, but each entry is a file symbolic link to the original
where the venue can create one, else a verified byte copy; the originals are pinned read-only for the
run; and every delete of a name this repository made goes through an identity check on the deleting
handle (Remove-AttrCudaFileById). This file is the static guard for the first two clauses:

  1. NO HARD-LINK CREATION ANYWHERE UNDER tools/. Any spelling -- CreateHardLink, New-Item
     -ItemType HardLink, fsutil hardlink create, mklink /H, os.link, Path.hardlink_to / link_to --
     in any tracked non-test source fails this test unless it is one of the named exceptions below,
     which carry the reason it cannot reach footage. Test files are exempt (a test needs a hard link
     to reproduce the hostile state on a synthetic folder); this file's own scan of them is not
     needed because a test never ships to a venue.
  2. NO WRITE, TRUNCATE OR APPEND THROUGH A NAME OF OWNER FOOTAGE. Every source that can reach an
     owner original, a view entry or a staged copy is scanned for each primitive that can write or
     truncate a file (FileMode.Create / Truncate / OpenOrCreate / Append, FileAccess.Write /
     ReadWrite, File.WriteAll* / AppendAll* / Create / Replace, Set-Content, Out-File, Add-Content,
     Clear-Content, Copy-Item -Force, New-Item -Force, and their aliases), and a hit fails unless
     it is one of the named exceptions. An exception is an exact source line with the reason its
     target cannot be an owner original or a view entry; a changed or added line is a failure until
     reviewed and listed, and an exception that matches nothing is a failure too.

The delete / rename class is pinned separately, for the same sources, by the I/O inventory
(test_attr3_footage_io_inventory.py), and the identity-checked delete itself is exercised on real
NTFS by test_owner_footage_identity_delete.py.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
BACHELOR = TOOLS / "profiling" / "bachelor"
THIS_FILE = Path(__file__).resolve()

# ---- 1. hard-link creation --------------------------------------------------------------------

SCANNED_SUFFIXES = {
    ".ps1", ".psm1", ".py", ".cs", ".cmd", ".bat", ".sh", ".js", ".mjs", ".yml", ".yaml",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
}

HARD_LINK_TEXT_PATTERNS = (
    r"\bCreateHardLink\w*",
    # -ItemType and every unambiguous prefix PowerShell binds to it (-I, -It, -Ite, -Item, -ItemT ...), plus the
    # -Type alias and its prefixes (-T, -Ty, -Typ).
    r"-(?:I(?:t(?:e(?:m(?:T(?:y(?:p(?:e)?)?)?)?)?)?)?|T(?:y(?:p(?:e)?)?)?)\s*:?\s*['\"]?HardLink\b",
    r"['\"]HardLink['\"]",
    r"\bcreate_hard_link\b",
    r"\bfs\.link(?:Sync)?\b",
    r"\bcp\s+(?:-\w*\s+)*-\w*l\w*\b",
    r"(?<![\w$.\-])ln\s+(?!-\w*s)[\w./\"'$~-]+\s+[\w./\"'$~-]+",
    r"\bNew-HardLink\b",
    r"\bfsutil(?:\.exe)?\s+hardlink\b",
    r"\bmklink(?:\.exe)?\s+/[hH]\b",
    r"\bFileLinkInformation\b",
    r"\b(?:Nt|Zw)SetInformationFile\b",
)
HARD_LINK_TEXT_RE = re.compile("|".join(f"(?:{p})" for p in HARD_LINK_TEXT_PATTERNS), re.IGNORECASE)
HARD_LINK_PY_ATTRIBUTES = {"link", "hardlink_to", "link_to", "CreateHardLink", "CreateHardLinkW"}

# (repo-relative path) -> why the hard link it creates can never be a name of owner footage.
HARD_LINK_ALLOWED = {
    "tools/coordination/record_workstream_completion.py":
        "atomic no-overwrite publish of a workstream-completion record: os.link(temporary, output) links a "
        "temp file THIS call just created (mkstemp, beside the output, holding only the record's own JSON) "
        "onto the output name, then the temp name is unlinked. It operates only on the ledger record under "
        ".claude-state; it never reads, names or is handed a footage path.",
    "tools/coordination/lane-dirty-save.ps1":
        "creates NO hard link: its one NtSetInformationFile call is the in-directory RENAME of the saver's own temp "
        "(DirtyFinalPath.MoveIn, compile-time constant info class 10 = FileRenameInformation, RootDirectory = the held "
        "run directory; kernel32's rename refuses a RootDirectory). It never passes 11 = FileLinkInformation, and its "
        "delete is kernel32 SetFileInformationByHandle class 4 = FileDispositionInfo, an enum with no link class. "
        "It only saves a lane worktree's git diff into a fleet run directory and cannot reach owner footage. "
        "test_the_lane_dirty_saver_keeps_exactly_its_one_rename_call pins the class, the count and the lines.",
}


def _strip_hash_comments(text: str) -> list[str]:
    """Source lines with '#' comments (quotes respected) and <# #> blocks removed, numbering kept."""
    text = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    out = []
    for line in text.splitlines():
        quote = None
        cut = len(line)
        for index, char in enumerate(line):
            if quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "#":
                cut = index
                break
        out.append(line[:cut].strip())
    return out


def _join_backtick_continuations(lines: list[str]) -> list[tuple[int, str]]:
    """(first line number, statement) with PowerShell backtick continuations joined onto their first line."""
    joined: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for number, line in enumerate(lines, start=1):
        if pending is not None:
            first, text = pending
            text = text.rstrip("`").rstrip() + " " + line
            if line.endswith("`"):
                pending = (first, text)
            else:
                joined.append((first, text))
                pending = None
            continue
        if line.endswith("`"):
            pending = (number, line)
        else:
            joined.append((number, line))
    if pending is not None:
        joined.append(pending)
    return joined


def _is_test_source(path: Path) -> bool:
    return path.name.startswith("test_") or "tests" in path.relative_to(ROOT).parts


HARD_LINK_SCAN_ROOTS = (TOOLS, ROOT / "platform", ROOT / "src", ROOT / ".github")


def _tool_sources() -> list[Path]:
    found = []
    for scan_root in HARD_LINK_SCAN_ROOTS:
        if not scan_root.is_dir():
            continue
        for path in sorted(scan_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            if {"__pycache__", "node_modules", ".git", "third_party", "3rdparty"} & set(path.parts):
                continue
            if path.resolve() == THIS_FILE or _is_test_source(path):
                continue
            found.append(path)
    return found


def hard_link_hits(path: Path) -> list[tuple[int, str]]:
    return hard_link_text_hits(path.read_text(encoding="utf-8", errors="ignore"), path.suffix.lower() == ".py")


def hard_link_text_hits(text: str, is_python: bool = False) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    if is_python:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                    if name in HARD_LINK_PY_ATTRIBUTES:
                        hits.append((node.lineno, ast.unparse(node)))
                    # getattr(os, "link") and friends
                    if name == "getattr" and any(
                            isinstance(arg, ast.Constant) and arg.value in HARD_LINK_PY_ATTRIBUTES for arg in node.args):
                        hits.append((node.lineno, ast.unparse(node)))
                # `os.link` passed as a reference (shutil.copytree(copy_function=os.link))
                if (isinstance(node, ast.Attribute) and node.attr in HARD_LINK_PY_ATTRIBUTES
                        and isinstance(node.value, ast.Name) and node.value.id in {"os", "pathlib", "ctypes", "kernel32"}):
                    hits.append((node.lineno, ast.unparse(node)))
                # mklink /H split across argv list elements
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "/h":
                    hits.append((node.lineno, repr(node.value)))
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and HARD_LINK_TEXT_RE.search(node.value):
                    hits.append((node.lineno, node.value.strip()[:120]))
            return sorted(set(hits))
    # A PowerShell backtick continuation puts the parameter name and its value on different physical lines
    # (`New-Item -ItemType `<newline>    HardLink ...`): join them first so a split spelling is still one statement.
    for number, line in _join_backtick_continuations(_strip_hash_comments(text)):
        if line and HARD_LINK_TEXT_RE.search(line):
            hits.append((number, line))
    return hits


# The lane dirty-saver's allowance is keyed by file, so this census is what keeps it narrow (PR #698 r12). On the RAW text
# (a comment mention counts too), every native set-information spelling must be the one declaration or the counted call,
# each call's info class is a literal from the named set, and no link name appears at all. The observed CI red was a
# census mismatch on a rename, not a demonstrated hard link; this keeps it that way.
LANE_DIRTY_SAVE = ROOT / "tools" / "coordination" / "lane-dirty-save.ps1"
LANE_DIRTY_SAVE_NT_CLASSES = {"10"}  # NtSetInformationFile: FileRenameInformation. 11 (FileLinkInformation) creates a hard link.
LANE_DIRTY_SAVE_NT_CALLS = 1
LANE_DIRTY_SAVE_K32_CLASSES = {"4"}  # SetFileInformationByHandle: FileDispositionInfo (that enum has no link class; pinned anyway).
# Every regex the saver census runs is case-INSENSITIVE: PowerShell resolves command, type and member names without case, so [T]::ntsetinformationfile, add-type
# and .getmethod(...) reach what the exact spellings name (r14 iter 1, adversary ea865d3b-A2). C# is case-sensitive, so a case variant there only fails closed.
_NATIVE_SET_INFO_RE = re.compile(r"\b(?:Nt|Zw)SetInformationFile\w*", re.IGNORECASE)
_K32_SET_INFO_RE = re.compile(r"\bSetFileInformationByHandle\w*", re.IGNORECASE)
# Every link spelling the wide scan knows, on the raw text (a comment or a string counts), so one appended to a counted line is never excused by it (r13,
# HARDLINK-CENSUS-SAME-LINE-SPELLING-1).
_LINK_NAME_RE = re.compile(
    r"FileLinkInfo\w*|CreateHardLink\w*|\bHardLink\b|\bmklink(?:\.exe)?\s+/[hH]\b|\bfs\.link(?:Sync)?\b|\bcreate_hard_link\b"
    r"|\bcp\s+(?:-\w*\s+)*-\w*l\w*\b|(?<![\w$.\-])ln\s+(?!-\w*s)[\w./\"'$~-]+\s+[\w./\"'$~-]+|\bfsutil(?:\.exe)?\s+hardlink\b",
    re.IGNORECASE)
# r13 (sol r12 B3, HARDLINK-CENSUS-ESCAPED-IDENTIFIER-1): a text census cannot follow reflection or a C# escaped identifier, so each FAILS CLOSED. A \u / \U
# escape anywhere (C# resolves it inside an identifier, NtSetInformationFile is the native), a verbatim identifier (@name) inside an Add-Type payload, a
# reflection lookup anywhere (GetMethod("NtSet" + "InformationFile", ...).Invoke(..., 11) needs no declaration or call the census counts) and a string literal
# joined to another inside a payload (a name built from pieces) are each a violation; the census below then also runs on the text with every escape resolved.
_CS_ESCAPE_RE = re.compile(r"\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}")
# r14 (Dot on 3e52cb20, HARDLINK-CENSUS-LITERAL-FORMAT-CHAR-1): C# drops a formatting (Cf) character from an identifier, so a LITERAL U+200C inside
# NtSet<ZWNJ>InformationFile binds the counted native while no regex here sees the name. The saver is ASCII-only, so ANY code point above 127 anywhere
# (comment, string or Add-Type payload) FAILS CLOSED; that covers every Cf character and identifier look-alike in one rule, with no normalization.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
_ADD_TYPE_PAYLOAD_RE = re.compile(r"Add-Type\b[^\n]*?@(['\"])\r?\n(?P<body>.*?)\r?\n\1@", re.DOTALL | re.IGNORECASE)
# The census reads the payload as written, so what Add-Type compiles must BE that text (r14 iter 1, adversary ea865d3b-A3: "(@'...'@ -replace ', 10\);', ', 11);')"
# left the checked call at 10 and compiled 11). The one Add-Type is exactly this line, it opens the one here-string in the file, and that here-string's
# terminator stands alone on its line, so no operator, parenthesis or continuation can touch the text between them.
LANE_DIRTY_SAVE_ADD_TYPE_LINE = "Add-Type -TypeDefinition @'"
_ADD_TYPE_ANY_RE = re.compile(r"Add-Type", re.IGNORECASE)
_HERE_STRING_OPEN_RE = re.compile(r"@['\"][ \t]*\r?$", re.MULTILINE)
_HERE_STRING_CLOSE_RE = re.compile(r"^['\"]@", re.MULTILINE)
# Code compiled, loaded or evaluated anywhere but that one payload is outside the census, so each such route fails closed (r14 iter 1, A3 siblings).
_DYNAMIC_CODE_RE = re.compile(
    r"Invoke-Expression|\biex\b|\[scriptblock\]::Create|NewScriptBlock|Assembly\]?::Load\w*|\bLoad(?:From|File)\b|CodeDom|CodeProvider|CodeAnalysis"
    r"|Import-Module|\bipmo\b", re.IGNORECASE)
# PowerShell can name a member or a command by a value computed at run time ($t.('Get'+'Method'), & ('Add-'+'Type')) or rebind a name (an alias or function
# called Add-Type), and no text census follows either, so each SITE of dynamic dispatch fails closed (r14 iter 2, adversary 27186cee-A1/A2). The saver's own
# shapes stay allowed: & $scriptblockVariable, & git, and Get-Command git -CommandType Application.
_DYNAMIC_DISPATCH_RE = re.compile(
    r"(?:\$\w+|[A-Za-z_\]\)])(?:\.|::)[(\$'\"]"  # a member named by an expression, a variable or a string
    r"|(?:^|[\s;|({=])&\s*[('\"]"  # the call operator on an expression or a string
    r"|(?:^|[;{])[ \t]*\.[ \t]+\S"  # dot-sourcing at the start of a statement
    r"|Set-Alias|New-Alias|Import-Alias|\bsal\b|\bnal\b|\balias:|\bfunction:"  # a name rebound (Add-Type -> a wrapper)
    r"|\bGet-Command\b(?! git -CommandType Application\))|\bgcm\b|InvokeCommand|ExecutionContext|SessionState|\bpsobject\b"
    r"|-MemberName\b|ForEach-Object\s+[('\"$]|\|\s*%\s+[('\"$]"  # a method invoked by a computed name through the pipeline
    r"|EncodedCommand|FromBase64", re.IGNORECASE | re.MULTILINE)
_VERBATIM_IDENTIFIER_RE = re.compile(r"(?<![\w\"'])@[A-Za-z_]")
_REFLECTION_RE = re.compile(
    r"\b(?:GetMethods?|GetMembers?|GetMember|GetRuntimeMethods?|GetDeclaredMethods?|GetTypeInfo|InvokeMember|MethodInfo|MethodBase|BindingFlags"
    r"|CreateDelegate|DynamicMethod|DynamicInvoke|System\.Reflection|Reflection\.Emit|Expression\.Call)\b", re.IGNORECASE)
_LITERAL_JOIN_RE = re.compile(r"\"\s*\+\s*@?\$?\"|'\s*\+\s*'")
# The parameter count of each counted native, so an extra trailing argument cannot move the class out of the checked position (r13, HARDLINK-CENSUS-ARITY-1).
LANE_DIRTY_SAVE_NT_ARITY = 5  # (handle, out iosb, info, length, infoClass)
LANE_DIRTY_SAVE_K32_ARITY = 4  # (handle, infoClass, ref info, size)
# Every native the saver binds, by its own name: an EntryPoint alias or a runtime lookup could bind NtSetInformationFile under any
# name (built from string pieces) and dodge the census (r12 iter 2, adversary 9ffb95df-A4).
LANE_DIRTY_SAVE_NATIVES = {
    "CreateFileW", "GetFinalPathNameByHandleW", "CloseHandle", "GetFileInformationByHandle", "NtCreateFile",
    "RtlNtStatusToDosError", "ReOpenFile", "NtSetInformationFile", "SetFileInformationByHandle",
}
_EXTERN_RE = re.compile(r"\bextern\b(?P<head>[^;(]*)\(", re.IGNORECASE)
_NATIVE_LOOKUP_RE = re.compile(
    r"\bEntryPoint\b|GetProcAddress|GetDelegateForFunctionPointer|\bNativeLibrary\b|\bLibraryImport\b|\bcalli\b", re.IGNORECASE)
# Each argument of a counted call or declaration is plain tokens only: a comment, string, directive or any other punctuation inside one could hide the ')' or
# ',' the parser counts, so "(uint)size /*, 10)*/, 11" read as class 10 while C# compiled 11 (r14 iter 1, adversary ea865d3b-A1).
_PLAIN_ARGUMENT_RE = re.compile(r"[A-Za-z0-9_ \t()]+")


def _call_arguments(text: str, at: int) -> list[str] | None:
    """Top-level arguments of the call whose '(' follows position `at` (whitespace allowed), or None when no call follows."""
    match = re.compile(r"\s*\(").match(text, at)
    if not match:
        return None
    depth, start, args = 1, match.end(), []
    for index in range(match.end(), len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                args.append(text[start:index].strip())
                return args
        elif char == "," and depth == 1:
            args.append(text[start:index].strip())
            start = index + 1
    return None


def _native_census(text: str, pattern: re.Pattern, declaration: str, class_at: int, allowed: set[str], arity: int):
    """(declaration lines, call lines, violations, spans of the counted name tokens) for one native entry point in `text`.

    The declaration and every call must have exactly `arity` parameters / arguments, and the class is read at its fixed position `class_at`
    (r13, HARDLINK-CENSUS-ARITY-1: an extra trailing argument used to leave a checked '10' last while 11 sat in the class position)."""
    declarations, calls, violations, spans = [], [], [], []
    for match in pattern.finditer(text):
        number = text.count("\n", 0, match.start()) + 1
        line = text.splitlines()[number - 1].strip()
        if match.group(0) != declaration:
            violations.append(f"{number}: a spelling other than {declaration}: {line}")
            continue
        args = _call_arguments(text, match.end())
        for arg in args or []:
            if not _PLAIN_ARGUMENT_RE.fullmatch(arg):
                violations.append(f"{number}: {declaration} has an argument that is not plain tokens (a comment, string or directive): {arg!r}")
        if re.search(r"\bstatic\s+extern\b[^\n;]*$", text[text.rfind("\n", 0, match.start()) + 1: match.start()]):
            declarations.append(number)
            spans.append(match.span())
            if args is None or len(args) != arity:
                violations.append(f"{number}: {declaration} declared with {len(args or [])} parameter(s), not {arity}: {line}")
            continue
        if args is None:
            violations.append(f"{number}: {declaration} named but not called (a string, delegate or comment): {line}")
            continue
        calls.append(number)
        spans.append(match.span())
        if len(args) != arity:
            violations.append(f"{number}: {declaration} called with {len(args)} argument(s), not {arity}: {line}")
        info_class = args[class_at] if 0 <= class_at < len(args) else ""
        if info_class not in allowed:
            violations.append(f"{number}: info class {info_class!r} is not a literal from {sorted(allowed)}: {line}")
    if len(declarations) != 1:
        violations.append(f"{declaration} must be declared exactly once, found lines {declarations}")
    return declarations, calls, violations, spans


def lane_dirty_save_violations(text: str) -> list[str]:
    violations = []
    for match in _NON_ASCII_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a non-ASCII character: U+{ord(match.group(0)):04X}")
    for match in _CS_ESCAPE_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: an escaped identifier character: {match.group(0)}")
    for match in _REFLECTION_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a reflection lookup: {match.group(0)}")
    for match in _DYNAMIC_CODE_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: code compiled, loaded or evaluated outside the one payload: {match.group(0)}")
    for match in _DYNAMIC_DISPATCH_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a member or command named at run time, or a name rebound: {match.group(0).strip()}")
    lines = text.splitlines()
    add_types = [text.count("\n", 0, m.start()) + 1 for m in _ADD_TYPE_ANY_RE.finditer(text)]
    opens = [text.count("\n", 0, m.start()) + 1 for m in _HERE_STRING_OPEN_RE.finditer(text)]
    closes = [text.count("\n", 0, m.start()) + 1 for m in _HERE_STRING_CLOSE_RE.finditer(text)]
    if len(add_types) != 1 or lines[add_types[0] - 1].strip() != LANE_DIRTY_SAVE_ADD_TYPE_LINE:
        violations.append(f"Add-Type must appear exactly once, as the line {LANE_DIRTY_SAVE_ADD_TYPE_LINE!r}; found lines {add_types}")
    if opens != add_types[:1]:
        violations.append(f"the one here-string must be the Add-Type payload; here-strings open on lines {opens}")
    if len(closes) != 1 or lines[closes[0] - 1].rstrip() != "'@":
        violations.append(f"the payload's terminator must stand alone on its line as '@; terminators on lines {closes}")
    for payload in _ADD_TYPE_PAYLOAD_RE.finditer(text):
        body, at = payload.group("body"), payload.start("body")
        for regex, what in ((_VERBATIM_IDENTIFIER_RE, "a verbatim identifier"), (_LITERAL_JOIN_RE, "a string built from pieces")):
            for match in regex.finditer(body):
                violations.append(f"{text.count(chr(10), 0, at + match.start()) + 1}: {what} inside an Add-Type payload: {match.group(0)}")
    # the census proper runs on the text C# compiles: every escape resolved
    text = _CS_ESCAPE_RE.sub(lambda m: chr(int(m.group(0)[2:], 16)), text)
    _, nt_calls, nt_violations, nt_spans = _native_census(
        text, _NATIVE_SET_INFO_RE, "NtSetInformationFile", LANE_DIRTY_SAVE_NT_ARITY - 1, LANE_DIRTY_SAVE_NT_CLASSES, LANE_DIRTY_SAVE_NT_ARITY)
    _, _, k32_violations, _ = _native_census(
        text, _K32_SET_INFO_RE, "SetFileInformationByHandle", 1, LANE_DIRTY_SAVE_K32_CLASSES, LANE_DIRTY_SAVE_K32_ARITY)
    violations += nt_violations + k32_violations
    if len(nt_calls) != LANE_DIRTY_SAVE_NT_CALLS:
        violations.append(f"NtSetInformationFile must be called exactly {LANE_DIRTY_SAVE_NT_CALLS} time(s), found lines {nt_calls}")
    for match in _LINK_NAME_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a link name: {match.group(0)}")
    for match in _NATIVE_LOOKUP_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a native bound by alias or at run time: {match.group(0)}")
    for match in _EXTERN_RE.finditer(text):
        name = (re.findall(r"\w+", match.group("head")) or [""])[-1]
        if name not in LANE_DIRTY_SAVE_NATIVES:
            violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: an extern outside the named set: {name!r}")
    # the file-keyed allowance covers exactly the declaration's and the counted call's NtSetInformationFile TOKENS, never their whole lines: those tokens are
    # blanked and the wide scan re-run, so another spelling on either line still counts (r13, HARDLINK-CENSUS-SAME-LINE-SPELLING-1)
    masked = text
    for start, end in nt_spans:
        masked = masked[:start] + " " * (end - start) + masked[end:]
    extra = [f"{number}: {line}" for number, line in hard_link_text_hits(masked)]
    violations += [f"a hard-link scan hit outside the counted rename: {hit}" for hit in extra]
    return violations


class NoHardLinkCreationTests(unittest.TestCase):
    def test_no_tracked_tool_creates_a_hard_link_except_the_named_exceptions(self) -> None:
        unexplained = []
        for path in _tool_sources():
            relative = path.relative_to(ROOT).as_posix()
            for number, line in hard_link_hits(path):
                if relative not in HARD_LINK_ALLOWED:
                    unexplained.append(f"{relative}:{number}: {line}")
        self.assertEqual(
            unexplained, [],
            "a tracked tool creates a hard link. A hard link is a second name of a file's bytes: any delete, "
            "sweep or truncate of it can destroy the original (OWNER-FOOTAGE-NO-HARDLINK-1). Use a symbolic "
            "link or a verified byte copy, or list the exact file in HARD_LINK_ALLOWED with the reason it "
            "cannot reach owner footage.")

    def test_the_ledger_publish_keeps_exactly_its_one_hard_link_call(self) -> None:
        # HARD_LINK_ALLOWED is keyed by file; a SECOND call in that file must not ride the allowance.
        ledger = ROOT / "tools" / "coordination" / "record_workstream_completion.py"
        self.assertEqual(len({number for number, _ in hard_link_hits(ledger)}), 1, hard_link_hits(ledger))

    def test_the_lane_dirty_saver_keeps_exactly_its_one_rename_call(self) -> None:
        # HARD_LINK_ALLOWED is keyed by file; this keeps the allowance to the one class-10 rename (PR #698 r12).
        self.assertEqual(lane_dirty_save_violations(LANE_DIRTY_SAVE.read_text(encoding="utf-8")), [])

    def test_the_lane_dirty_saver_census_goes_red_on_every_widening(self) -> None:
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        call = re.search(r"NtSetInformationFile\(h, out io, info, \(uint\)size, 10\);", text)
        self.assertIsNotNone(call, "the census mutants below are cut from this exact call")
        call = call.group(0)
        mutants = {
            # (i) class 11 / FileLinkInformation at any site
            "class 11": text.replace(call, call.replace(", 10)", ", 11)")),
            "a FileLinkInformation name": text.replace(call, call + " // FileLinkInformation"),
            "a disposition call widened to 11": text.replace("SetFileInformationByHandle(h, 4,", "SetFileInformationByHandle(h, 11,", 1),
            # (ii) an unknown or dynamic class
            "a variable class": text.replace(call, call.replace(", 10)", ", infoClass)")),
            "an expression class": text.replace(call, call.replace(", 10)", ", 5 + 5)")),
            "another constant": text.replace(call, call.replace(", 10)", ", 13)")),
            "the same value spelled in hex": text.replace(call, call.replace(", 10)", ", 0x0A)")),
            "a dynamic disposition class": text.replace("SetFileInformationByHandle(h, 4,", "SetFileInformationByHandle(h, cls,", 1),
            # (iii) an additional link-capable call site or entry point
            "a second rename call": text.replace(call, call + "\n" + call),
            "a second declaration": text.replace(
                "static extern int NtSetInformationFile(", "static extern int NtSetInformationFile(IntPtr a);\n    static extern int NtSetInformationFile(", 1),
            "the Zw spelling": text.replace(call, call + "\n" + call.replace("NtSet", "ZwSet")),
            "an EntryPoint alias": text.replace(call, call + '\n[DllImport("ntdll.dll", EntryPoint = "NtSetInformationFile")] static extern int Quiet();'),
            "a mention in a comment": text.replace(call, call + " // NtSetInformationFile again"),
            "a CreateHardLinkW call": text.replace(call, call + "\nCreateHardLinkW(a, b, IntPtr.Zero);"),
            "a New-Item hard link": text + "\nNew-Item -ItemType HardLink -Path $a -Value $b\n",
            # adversary 9ffb95df-A4: a native bound under another name, its entry point built from string pieces
            "a concatenated EntryPoint alias": text.replace(call, call + '\n[DllImport("ntdll.dll", EntryPoint = "NtSet" + "InformationFile")] '
                                                          "static extern int Alias(IntPtr h, out IoStatusBlock io, IntPtr p, uint l, int c);\nAlias(h, out io, info, 24, 11);"),
            "a concatenated disposition alias": text.replace(call, call + '\n[DllImport("kernel32.dll", EntryPoint = "SetFileInformation" + "ByHandle")] '
                                                            "static extern bool Quiet(IntPtr h, int c, IntPtr p, uint s);\nQuiet(h, 999, info, 24);"),
            "an extern outside the named set": text.replace(call, call + '\n[DllImport("ntdll.dll")] static extern int NtQuietSet(IntPtr h, int c);'),
            "a runtime lookup": text.replace(call, call + '\nIntPtr p = GetProcAddress(GetModuleHandle("ntdll.dll"), "NtSet" + "InformationFile");'),
            # r13 (sol r12 B3, HARDLINK-CENSUS-ESCAPED-IDENTIFIER-1): reflection and escaped identifiers reach the native with no counted spelling
            "a split-name reflective invoke": text.replace(call, 'typeof(DirtyFinalPath).GetMethod("NtSet"+"InformationFile", System.Reflection.BindingFlags.Static '
                                                         "| System.Reflection.BindingFlags.NonPublic).Invoke(null,new object[]{h,default(IoStatusBlock),info,(uint)size,11});\n" + call),
            "a reflective invoke through a variable name": text.replace(call, 'string n="NtSet"+"InformationFile"; typeof(DirtyFinalPath).GetMethod(n,System.Reflection.BindingFlags.Static '
                                                                       "| System.Reflection.BindingFlags.NonPublic).Invoke(null,new object[]{h,default(IoStatusBlock),info,(uint)size,11});\n" + call),
            "a reflective invoke with numeric flags": text.replace(call, 'var m=typeof(DirtyFinalPath).GetMethod("NtSet"+"InformationFile",(System.Reflection.BindingFlags)40); '
                                                                  "m.Invoke(null,new object[]{h,default(IoStatusBlock),info,(uint)size,11});\n" + call),
            "a reflective InvokeMember": text.replace(call, 'typeof(DirtyFinalPath).InvokeMember(N, (System.Reflection.BindingFlags)296, null, null, a);\n' + call),
            "a \\u-escaped identifier": text.replace(call, call + "\nN\\u0074SetInformationFile(h, out io, info, (uint)size, 11);"),
            "a \\U-escaped identifier": text.replace(call, call + "\nN\\U00000074SetInformationFile(h, out io, info, (uint)size, 11);"),
            "a verbatim identifier": text.replace(call, "@" + call),
            "a name joined from literals in the payload": text.replace(call, call + '\nstring q = "NtSet" + "InformationFile";'),
            # r13 (HARDLINK-CENSUS-ARITY-1): one more parameter puts 11 in the class position while the last argument stays 10
            "an extra trailing parameter": text.replace(
                "static extern int NtSetInformationFile(IntPtr handle, out IoStatusBlock iosb, IntPtr info, uint length, int infoClass);",
                "static extern int NtSetInformationFile(IntPtr handle, out IoStatusBlock iosb, IntPtr info, uint length, int infoClass, int reserved);").replace(
                call, call.replace(", 10)", ", 11, 10)")),
            "a call with one argument too many": text.replace(call, call.replace(", 10)", ", 11, 10)")),
            "a disposition call with one argument too many": text.replace("SetFileInformationByHandle(h, 4, ref d, 1)", "SetFileInformationByHandle(h, 11, 4, ref d, 1)", 1),
            # r13 (HARDLINK-CENSUS-SAME-LINE-SPELLING-1): another spelling on the counted call or declaration line is not excused by its line number
            "mklink /H on the call line": text.replace(call, call + ' Process.Start("cmd", "/c mklink /H a b");'),
            "fs.link on the call line": text.replace(call, call + " // fs.link(a, b)"),
            "create_hard_link on the declaration line": text.replace(
                "static extern int NtSetInformationFile(IntPtr handle, out IoStatusBlock iosb, IntPtr info, uint length, int infoClass);",
                "static extern int NtSetInformationFile(IntPtr handle, out IoStatusBlock iosb, IntPtr info, uint length, int infoClass); // create_hard_link"),
            "ln on the call line": text.replace(call, call + ' Process.Start("sh", "-c \\"ln src dst\\"");'),
            "cp -l on the call line": text.replace(call, call + ' Process.Start("sh", "-c \\"cp -al src dst\\"");'),
        }
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                self.assertTrue(lane_dirty_save_violations(mutant), f"the census missed: {name}")

    def test_the_lane_dirty_saver_census_rejects_a_literal_non_ascii_character(self) -> None:
        # r14 (Dot on 3e52cb20, HARDLINK-CENSUS-LITERAL-FORMAT-CHAR-1): C# removes a Cf character from an identifier, so each literal below binds the counted
        # native beside the original declaration and class-10 call while the name regex sees nothing. Built with chr() so this file stays ASCII.
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        call = re.search(r"NtSetInformationFile\(h, out io, info, \(uint\)size, 10\);", text).group(0)

        def extra(name: str, info_class: str) -> str:
            return text.replace(call, call + f"\n{name}(h, out io, info, (uint)size, {info_class});")

        mutants = {
            "a literal ZWNJ extra call with class 11": (extra("NtSet" + chr(0x200C) + "InformationFile", "11"), 0x200C),
            "a literal ZWNJ extra call with class 10": (extra("NtSet" + chr(0x200C) + "InformationFile", "10"), 0x200C),
            "a literal ZWJ in the name": (extra("NtSet" + chr(0x200D) + "InformationFile", "11"), 0x200D),
            "a literal soft hyphen in the name": (extra("NtSet" + chr(0x00AD) + "InformationFile", "11"), 0x00AD),
            "a literal U+FEFF in the name": (extra("NtSet" + chr(0xFEFF) + "InformationFile", "11"), 0xFEFF),
            "a non-ASCII character in a comment only": (text.replace(call, call + " // caf" + chr(0x00E9)), 0x00E9),
        }
        for name, (mutant, code_point) in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                line = mutant.count("\n", 0, mutant.index(chr(code_point))) + 1
                self.assertIn(f"{line}: a non-ASCII character: U+{code_point:04X}", lane_dirty_save_violations(mutant), f"the census missed: {name}")
        # control: an ASCII renamed extra call is still caught by the spelling rule, and is not reported as non-ASCII
        ascii_rename = extra("NtSetInformationFileX", "11")
        violations = lane_dirty_save_violations(ascii_rename)
        self.assertTrue(any("a spelling other than NtSetInformationFile" in v for v in violations), violations)
        self.assertFalse(any("non-ASCII" in v for v in violations), violations)
        self.assertEqual(sum(byte > 127 for byte in THIS_FILE.read_bytes()), 0, "this test file must stay ASCII")

    def test_the_lane_dirty_saver_census_rejects_what_the_r14_adversary_compiled_past_it(self) -> None:
        # r14 iter 1 (adversary ea865d3b): each input below returned [] while C# or PowerShell would reach class 11 or an uncounted native.
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        call = "NtSetInformationFile(h, out io, info, (uint)size, 10);"
        dispose = "SetFileInformationByHandle(h, 4, ref d, 1)"
        payload_close = "\n'@\n"
        self.assertIn(call, text)
        self.assertIn(dispose, text)
        self.assertIn(payload_close, text)
        mutants = {
            # A1: comment punctuation inside the arguments moved the counted class
            "A1 a block comment hides the class": text.replace(call, "NtSetInformationFile(h, out io, info, (uint)size /*, 10)*/, 11);"),
            "A1 a disposition block comment": text.replace(dispose, "SetFileInformationByHandle(h /*, 4, ref d, 1)*/, 11, ref d, 1)", 1),
            "A1 a line comment hides the class": text.replace(call, "NtSetInformationFile(h, out io, info, (uint)size //, 10)\n, 11);"),
            "A1 a directive picks the class": text.replace(call, "NtSetInformationFile(h, out io, info, (uint)size,\n#if NEVER\n10\n#else\n11\n#endif\n);"),
            "A1 a string in an argument": text.replace(call, 'NtSetInformationFile(h, out io, info, (uint)Q(")"), 10);'),
            # A2: PowerShell resolves names without case
            "A2 a lowercase reflective getmethod": text + "\n[DirtyFinalPath].getmethod(('NtSet'+'InformationFile'),40)\n",
            "A2 a lowercase nativelibrary load": text + "\n[system.runtime.interopservices.nativelibrary]::load('ntdll.dll');\n",
            "A2 a lowercase add-type with a verbatim identifier": text.replace(
                "Add-Type -TypeDefinition @'", "add-type -TypeDefinition @'").replace(call, call + "\npublic static int @CensusProbe = 0;"),
            "A2 a lowercase native call from PowerShell": text + "\n[DirtyFinalPath]::ntsetinformationfile($h, [ref]$io, $p, 24, 11)\n",
            "A2 a lowercase extern outside the named set": text.replace(call, call + '\n[DllImport("ntdll.dll")] static EXTERN int NtQuietSet(IntPtr h, int c);'),
            "A2 a lowercase getprocaddress": text + "\n$p = [K]::getprocaddress($m, 'x')\n",
            # A3: the compiled text differs from the checked text
            "A3 a -replace on the payload": text.replace("Add-Type -TypeDefinition @'", "Add-Type -TypeDefinition (@'").replace(
                payload_close, "\n'@ -replace ', 10\\);', ', 11);')\n", 1),
            "A3 a payload from a variable": text.replace("Add-Type -TypeDefinition @'", "$src = @'").replace(
                payload_close, "\n'@\nAdd-Type -TypeDefinition ($src -replace ', 10\\);', ', 11);')\n", 1),
            "A3 a second Add-Type from a path": text + "\nAdd-Type -Path $env:TEMP\\x.cs\n",
            "A3 a second here-string": text + "\n$x = @'\nNtSetInformationFile\n'@\n",
            "A3 an Invoke-Expression": text + "\nInvoke-Expression ('Add-' + 'Type -TypeDefinition $s')\n",
            "A3 a lowercase iex": text + "\niex $s\n",
            "A3 a scriptblock create": text + "\n& ([scriptblock]::Create($s))\n",
            "A3 an assembly load": text + "\n[Reflection.Assembly]::LoadFile($dll)\n",
            "A3 an Import-Module of a dll": text + "\nImport-Module $dll\n",
        }
        # r14 iter 2 (adversary 27186cee): a member or command named at run time, or Add-Type rebound to a wrapper, inserted into the real saver
        lines = text.splitlines(keepends=True)
        at_line_50 = lambda inserted: "".join(lines[:49]) + inserted + "\n" + "".join(lines[49:])  # noqa: E731
        mutants.update({
            "iter2 A1 a reflective GetMethod built from pieces": at_line_50(
                "$t = [DirtyFinalPath]; $m = $t.('Get'+'Method')(('NtSet'+'InformationFile'),40); "
                "$null = $m.Invoke($null,[object[]]@([IntPtr]::Zero,$null,[IntPtr]::Zero,[uint32]0,11))"),
            "iter2 A1 a member named by a variable": at_line_50("$n = 'Get' + 'Method'; $m = [DirtyFinalPath].$n('x', 40)"),
            "iter2 A1 a member named by a string": at_line_50("$m = [DirtyFinalPath].'GetMethod'('x', 40)"),
            "iter2 A1 a static member named by an expression": at_line_50("[DirtyFinalPath]::('Re'+'solve')($p)"),
            "iter2 A1 a member invoked through ForEach-Object": at_line_50("$m = [DirtyFinalPath] | ForEach-Object ('Get'+'Method') 'x' 40"),
            "iter2 A1 a member invoked through -MemberName": at_line_50("$m = [DirtyFinalPath] | ForEach-Object -MemberName $n 'x' 40"),
            "iter2 A1 a computed scriptblock": at_line_50("$sb = $ExecutionContext.InvokeCommand.NewScriptBlock($s); & $sb"),
            "iter2 A1 a computed module import": at_line_50("& ('Import-'+'Module') $dll"),
            "iter2 A1 an encoded child": at_line_50("& pwsh -EncodedCommand $b"),
            "iter2 A1 a dot-sourced script": at_line_50(". $env:TEMP\\x.ps1"),
            "iter2 A1 a psobject method lookup": at_line_50("$m = $t.psobject.Methods['GetMethod']"),
            "iter2 A2 Add-Type rebound by a computed alias": text + (
                "\nfunction Invoke-CensusSwap {\n    param([string]$TypeDefinition)\n"
                "    & ('Microsoft.PowerShell.Utility\\Add-'+'Type') -TypeDefinition ($TypeDefinition -replace ', 10\\);', ', 11);')\n}\n"
                "Set-Alias -Name ('Add-'+'Type') -Value Invoke-CensusSwap\n"),
            "iter2 A2 an alias by New-Alias": text + "\nNew-Alias -Name $n -Value Invoke-CensusSwap\n",
            "iter2 A2 a lowercase sal": text + "\nsal $n Invoke-CensusSwap\n",
            "iter2 A2 a function drive entry": text + "\nNew-Item -Path function:\\$n -Value { param($TypeDefinition) }\n",
            "iter2 A2 a Get-Command lookup": text + "\n& (Get-Command ('Add-'+'Type'))\n",
            "iter2 A2 a lowercase gcm": text + "\n& (gcm *-Type)\n",
        })
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                self.assertTrue(lane_dirty_save_violations(mutant), f"the census missed: {name}")

    def test_every_named_hard_link_exception_still_matches(self) -> None:
        present = {path.relative_to(ROOT).as_posix() for path in _tool_sources() if hard_link_hits(path)}
        stale = sorted(key for key in HARD_LINK_ALLOWED if key not in present)
        self.assertEqual(stale, [], "an exception no longer matches any hard-link call: delete it from HARD_LINK_ALLOWED")

    def test_the_owner_footage_workspace_never_mentions_creating_one_in_code(self) -> None:
        for name in ("AttrCudaOwnerFootage.psm1", "playback-attr-3-cuda-job.ps1", "Attr3FootageStageJob.psm1"):
            with self.subTest(source=name):
                self.assertEqual(hard_link_hits(BACHELOR / name), [])

    def test_the_scan_catches_every_spelling_it_claims_to(self) -> None:
        must_hit_text = [
            "New-Item -ItemType HardLink -Path $a -Value $b",
            "New-Item -Path $a -ItemType hardlink -Value $b",
            "[void][Kernel]::CreateHardLinkW($a, $b, [IntPtr]::Zero)",
            "fsutil hardlink create $a $b",
            "fsutil.exe hardlink create $a $b",
            "cmd /c mklink /H $a $b",
            "$info = [FileLinkInformation]::new()",
            "NtSetInformationFile($h, $io, $p, 24, 11)",
            "ZwSetInformationFile($h, $io, $p, 24, 11)",
            "New-Item -ItemType 'HardLink' -Path $a -Value $b",
            "New-Item -ItemType:HardLink -Path $a -Value $b",
            "New-Item -Type HardLink -Path $a -Value $b",
            "New-Item -I HardLink -Path $a -Value $b",
            "$kind = 'HardLink'",
            "std::filesystem::create_hard_link(a, b);",
            "fs.linkSync(a, b)",
            "cp -al a b",
            "ln a b",
            "New-Item -Item HardLink -Path $a -Value $b",
            "New-Item -ItemT HardLink -Path $a -Value $b",
            "New-Item -Ty HardLink -Path $a -Value $b",
            "New-Item -Typ:HardLink -Path $a -Value $b",
        ]
        for line in must_hit_text:
            self.assertTrue(HARD_LINK_TEXT_RE.search(line), f"the scan missed: {line}")
        for line in (
            "New-Item -ItemType SymbolicLink -Path $a -Target $b",
            "New-Item -ItemType Junction -Path $a -Target $b",
            "$hardLinkCount = 2",
            "Get-AttrCudaFileId -Path $a",
            "mklink $a $b",
            "ln -s a b",
            "cp a b",
        ):
            self.assertFalse(HARD_LINK_TEXT_RE.search(line), f"false positive: {line}")
        # the Python side is structural, not textual (references, getattr and split argv count too)
        for referenced in (
            "import os, shutil\nshutil.copytree('a', 'b', copy_function=os.link)\n",
            "import os\ngetattr(os, 'link')('a', 'b')\n",
            "import subprocess\nsubprocess.run(['cmd', '/c', 'mklink', '/H', 'a', 'b'])\n",
        ):
            tree = ast.parse(referenced)
            found = []
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr in HARD_LINK_PY_ATTRIBUTES
                        and isinstance(node.value, ast.Name) and node.value.id in {"os", "pathlib", "ctypes", "kernel32"}):
                    found.append(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and any(
                        isinstance(arg, ast.Constant) and arg.value in HARD_LINK_PY_ATTRIBUTES for arg in node.args):
                    found.append(node)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "/h":
                    found.append(node)
            self.assertTrue(found, f"the structural scan would miss: {referenced!r}")
        source = "import os\nfrom pathlib import Path\nos.link('a', 'b')\nPath('a').hardlink_to('b')\nPath('a').link_to('b')\n"
        tree = ast.parse(source)
        names = [
            (n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id)
            for n in ast.walk(tree) if isinstance(n, ast.Call)
        ]
        self.assertEqual(sorted(name for name in names if name in HARD_LINK_PY_ATTRIBUTES), ["hardlink_to", "link", "link_to"])

    def test_a_backtick_continued_spelling_is_still_caught(self) -> None:
        # sol r2 (OWNER-NO-HARDLINK-CONTINUATION-GUARD-1): the scanner examined physical lines, so a
        # continuation between -ItemType and an UNQUOTED HardLink returned no hit.
        import tempfile

        samples = {
            "split_after_param.ps1": "New-Item -ItemType `\n    HardLink -Path $a -Value $b\n",
            "split_before_value.ps1": "New-Item `\n    -ItemType HardLink `\n    -Path $a `\n    -Value $b\n",
            "split_prefix.ps1": "New-Item -Item `\n    HardLink -Path $a -Value $b\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in samples.items():
                path = Path(tmp) / name
                path.write_text(source, encoding="utf-8")
                self.assertTrue(hard_link_hits(path), f"the scan missed a continued spelling: {name}")
            clean = Path(tmp) / "clean.ps1"
            clean.write_text("New-Item -ItemType `\n    SymbolicLink -Path $a -Target $b\n", encoding="utf-8")
            self.assertEqual(hard_link_hits(clean), [])

    def test_the_scan_now_walks_the_native_and_ci_roots_too(self) -> None:
        roots = {path.relative_to(ROOT).parts[0] for path in _tool_sources()}
        self.assertIn("tools", roots)
        self.assertTrue({"platform", "src", ".github"} & roots, "the native / CI roots are outside the scan")

    def test_the_scan_covers_the_sources_the_owner_jobs_are_built_from(self) -> None:
        scanned = {path.name for path in _tool_sources()}
        for required in ("AttrCudaOwnerFootage.psm1", "AttrCudaArtifacts.psm1", "Attr3FootageStageJob.psm1",
                         "playback-attr-3-cuda-job.ps1", "attr3-footage-stage.ps1", "record_workstream_completion.py"):
            self.assertIn(required, scanned)


# ---- 2. write / truncate / append through a name of owner footage ------------------------------

OWNER_FOOTAGE_SOURCES = (
    BACHELOR / "AttrCudaOwnerFootage.psm1",
    BACHELOR / "playback-attr-3-cuda-job.ps1",
    BACHELOR / "Attr3FootageStageJob.psm1",
    BACHELOR / "attr3-footage-stage.ps1",
    BACHELOR / "Attr3FootagePresenceJob.psm1",
    BACHELOR / "attr3-footage-presence-job.ps1",
    BACHELOR / "Attr3FootageReadRateJob.psm1",
    BACHELOR / "attr3-footage-read-rate-job.ps1",
)

# Every spelling that can write, truncate or append to a file, or open one that way -- including the
# PowerShell aliases (sc, ac, clc, ni, cpi, copy, cp) and redirection.
WRITE_PRIMITIVES = (
    r"\[(?:System\.)?IO\.FileMode\]::(?:Create|Truncate|OpenOrCreate|Append)\b",
    r"\[(?:System\.)?IO\.FileAccess\]::(?:Write|ReadWrite)\b",
    r"\[(?:System\.)?IO\.File\]::(?:WriteAll\w+|AppendAll\w+|Create|CreateText|AppendText|Replace|OpenWrite)\b",
    r"\b(?:Set|Add|Clear)-Content\b",
    r"\bOut-File\b",
    r"\bCopy-Item\b",
    r"\[(?:System\.)?IO\.File\]::Copy\s*\(",
    r"\bStreamWriter\b",
    r"\bTee-Object\b",
    r"\bExport-(?!ModuleMember\b)\w+\b",
    r"-Redirect(?:StandardOutput|StandardError)\b",
    r"(?:\.|::)Open\s*\([^)]*['\"](?:Truncate|Append|Create|OpenOrCreate)['\"]",
    r"\bNew-Item\b(?![^\n]*-ItemType\s+Directory)[^\n]*-Force",
    r"\bSet-ItemProperty\b",
    r"\bWriteByte\b",
    r"\.Write\s*\(",
    r"\.SetLength\s*\(",
    r"(?<![\w$.-])(?:sc|ac|clc|ni|cpi|copy|cp)(?![\w-])\s+-",
)
# Output redirection is matched on the line with its quoted strings blanked out (a '<none>' literal is not one).
REDIRECTION_RE = re.compile(r"(?<![<>=-])>>?(?![=>&])\s*(?!\$null\b)\S")
WRITE_RE = re.compile("|".join(f"(?:{p})" for p in WRITE_PRIMITIVES), re.IGNORECASE)

# A line that names a path of owner footage AND a write primitive is never acceptable, allowlist or not.
OWNER_PATH_TOKENS = re.compile(
    r"\$(?:OwnerClipDir|linkPath|clipPath|partPath|ownerVerifiedParts|ownerAssertedParts|ownerDecodedParts|"
    r"pin|view|viewPath|SourcePath|targetPath|stagedPath|localPartialPath)\b|\$part\.path\b|\$rawPart\b",
    re.IGNORECASE,
)

# (file name, the source line with comments removed and whitespace trimmed) -> why its target cannot be
# an owner original, a view entry or a name of owner footage. Exactly these, nothing else.
WRITE_ALLOWED = {
    # ---- AttrCudaOwnerFootage.psm1 -------------------------------------------------------------
    ("AttrCudaOwnerFootage.psm1", "$destStream = [IO.File]::Open($partialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "Send-AttrCudaOwnerFootagePartToStaging: the '<slot>.partial' BYTE COPY on the agent share, created by this "
        "call with FileMode.CreateNew (refuses to touch anything already there) in the per-job staging directory; "
        "never an owner original, never a view entry.",
    ("AttrCudaOwnerFootage.psm1", "$stream = [IO.File]::Open($targetPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "Test-AttrCudaSymlinkCapability: the throwaway 1-byte probe target, created by this call with CreateNew "
        "under a GUID name inside the job's own view directory.",
    ("AttrCudaOwnerFootage.psm1", "$stream.Write($probeBytes, 0, $probeBytes.Length)"):
        "Test-AttrCudaSymlinkCapability: writes the 4097 probe bytes to that same CreateNew handle.",
    ("AttrCudaOwnerFootage.psm1", "$destStream = [IO.File]::Open($viewPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "New-AttrCudaOwnerFootageView (copy mode): the VIEW COPY, created with CreateNew under the neutral name in "
        "the job-private view directory. It is a separate file object with one name; the original is only READ "
        "(through the read-only pin) and is never opened for write.",
    # ---- playback-attr-3-cuda-job.ps1 (generator + emitted template) --------------------------
    # Everything the emitted job writes goes through Publish-AttrCuda* into $Pub / $Work (the publish-write
    # scan tools/repo_hygiene/attr3_publish_write_scan.ps1 proves the destinations); the taint check below
    # proves no write line names an owner path. The one write primitive left in the file is the generator's own:
    ("playback-attr-3-cuda-job.ps1", "[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))"):
        "generator side: writes the emitted job FILE at its own -OutFile; not a job-runtime write and not footage.",
    ("playback-attr-3-cuda-job.ps1", "& \"$env:ProgramFiles\\PowerShell\\7\\pwsh.exe\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $cmd 1> (Join-Path $legOut 'smoke-stdout.txt') 2> (Join-Path $legOut 'smoke-stderr.txt')"):
        "the smoke child's stdout / stderr redirected into two fixed-name files under $legOut, the job's own "
        "per-leg output directory inside $Pub (created by this job a few lines above, unique per job); not a "
        "view entry and not an owner path.",
    ("playback-attr-3-cuda-job.ps1", "$proc = Start-Process -FilePath (Join-Path $Cache $PresentMonName) -ArgumentList $pmArgs -RedirectStandardOutput $presentMonStdoutPath -RedirectStandardError $presentMonStderrPath -PassThru -WindowStyle Hidden"):
        "DVE-PRESENTMON-EVIDENCE-1: PresentMon's own stdout / stderr redirected into two fixed-name files ('presentmon-stdout.txt', "
        "'presentmon-stderr.txt') under $legOut, the job's own per-leg output directory (each variable is a single "
        "Join-Path $legOut '<literal>' assignment, which the publish-write scan proves is under $Work); PresentMon is "
        "launched with a process name and its own CSV path, never an owner path, and never opens footage.",
    ("playback-attr-3-cuda-job.ps1", "$rows | Export-Csv -LiteralPath (Join-Path $legOut 'probe-timeline.csv') -NoTypeInformation"):
        "the probe timeline CSV, a fixed-name file in $legOut -- the job's own per-leg output directory inside $Pub, "
        "created by this job and unique per job; the rows are the job's own measurements, and the path names no "
        "owner path and no view entry.",
    ("playback-attr-3-cuda-job.ps1", "$pmRows | Export-Csv -LiteralPath (Join-Path $legOut 'presentmon-series.csv') -NoTypeInformation"):
        "the PresentMon series CSV, a fixed-name file in the same per-leg output directory; not an owner path.",
    # ---- Attr3FootageStageJob.psm1 -------------------------------------------------------------
    ("Attr3FootageStageJob.psm1", "$localDstStream = [IO.File]::Open($localPartialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "the target-volume partial, created by THIS attempt with CreateNew under a per-attempt name carrying the "
        "job id; identified by the identity read off this very handle.",
    ("Attr3FootageStageJob.psm1", "$corruptStream = [IO.File]::Open($localPartialPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "TEST-ONLY hook (-TestHookCorruptAfterVerifyPartIndex, -1 in production): appends one byte to the job's OWN "
        "verified partial before the publish rename, to prove the post-rename re-hash catches it.",
    ("Attr3FootageStageJob.psm1", "try { $corruptStream.WriteByte(0) } finally { $corruptStream.Dispose() }"):
        "the same test-only hook: writes that one byte to the partial opened on the line above.",
    ("Attr3FootageStageJob.psm1", "[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))"):
        "generator side: writes the emitted job FILE (<job id>.job.ps1) into the caller's -OutDir; not footage.",
    # ---- Attr3FootagePresenceJob.psm1 / the two generators' own output ------------------------
    ("Attr3FootagePresenceJob.psm1", "[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))"):
        "generator side: writes the emitted presence job FILE into -OutDir; not footage.",
    ("Attr3FootageReadRateJob.psm1", "[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))"):
        "generator side: writes the emitted read-rate job FILE into -OutDir; not footage.",
}

# Lines that name an owner-path variable next to a write primitive and are still acceptable, with the reason.
TAINT_EXEMPT = {
    ("Attr3FootageStageJob.psm1", "$corruptStream = [IO.File]::Open($localPartialPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::None)"):
        "TEST-ONLY hook (-TestHookCorruptAfterVerifyPartIndex, -1 in production): $localPartialPath is the job's OWN "
        "per-attempt partial, created by this attempt with CreateNew and identified by the identity read off that "
        "handle; it is never an owner original or a staged copy.",
}


def _line_writes(line: str) -> bool:
    if WRITE_RE.search(line):
        return True
    return bool(REDIRECTION_RE.search(re.sub(r"'[^']*'|\"[^\"]*\"", "''", line)))


def write_hits(path: Path) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(_strip_hash_comments(path.read_text(encoding="utf-8")), start=1)
        if line and _line_writes(line)
    ]


class NoWriteThroughOwnerFootageTests(unittest.TestCase):
    def test_every_source_exists(self) -> None:
        for path in OWNER_FOOTAGE_SOURCES:
            self.assertTrue(path.is_file(), path)

    def test_no_write_primitive_is_reachable_except_the_named_exceptions(self) -> None:
        unexplained = []
        for path in OWNER_FOOTAGE_SOURCES:
            for number, line in write_hits(path):
                if (path.name, line) not in WRITE_ALLOWED:
                    unexplained.append(f"{path.name}:{number}: {line}")
        self.assertEqual(
            unexplained, [],
            "a write / truncate / append primitive appeared in a source that can reach a name of owner footage. "
            "Nothing that handles owner footage may open an original, a view entry or a staged copy for write; "
            "if the line cannot reach one, add it to WRITE_ALLOWED with the reason.")

    def test_every_named_write_exception_still_matches_a_line(self) -> None:
        present = {(path.name, line) for path in OWNER_FOOTAGE_SOURCES for _, line in write_hits(path)}
        stale = sorted(key for key in WRITE_ALLOWED if key not in present)
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from WRITE_ALLOWED")

    def test_no_line_pairs_a_write_primitive_with_an_owner_path(self) -> None:
        offenders = []
        for path in OWNER_FOOTAGE_SOURCES:
            for number, line in write_hits(path):
                if (OWNER_PATH_TOKENS.search(line) and "FileMode]::CreateNew" not in line
                        and (path.name, line) not in TAINT_EXEMPT):
                    offenders.append(f"{path.name}:{number}: {line}")
        self.assertEqual(offenders, [], "a write primitive names an owner-footage path variable")

    def test_the_only_writes_to_an_owner_path_are_exclusive_creations(self) -> None:
        # The allowlist above may only contain a FileAccess.Write / Append open when it is a CreateNew of a
        # name this code makes itself (or the named test hook); no Open/OpenOrCreate/Truncate/Create.
        for (name, line), reason in WRITE_ALLOWED.items():
            with self.subTest(source=name, line=line):
                if re.search(r"FileMode\]::(?:Open|OpenOrCreate|Truncate|Create)\b", line):
                    self.fail("an exception opens an existing name for write or truncates it")

    def test_the_scanner_catches_every_alias_and_spelling_it_claims_to(self) -> None:
        must_hit = [
            "$s = [IO.File]::Open($p, [IO.FileMode]::Create, [IO.FileAccess]::Write)",
            "$s = [IO.File]::Open($p, [IO.FileMode]::Truncate)",
            "$s = [IO.File]::Open($p, [IO.FileMode]::OpenOrCreate)",
            "$s = [IO.File]::Open($p, [IO.FileMode]::Append)",
            "$s = [System.IO.File]::Open($p, 'Open', [System.IO.FileAccess]::ReadWrite)",
            "[IO.File]::WriteAllBytes($p, $b)",
            "[IO.File]::WriteAllText($p, $t)",
            "[IO.File]::AppendAllText($p, $t)",
            "[IO.File]::Create($p)",
            "[System.IO.File]::Replace($a, $b, $null)",
            "Set-Content -LiteralPath $p -Value x",
            "Add-Content -LiteralPath $p x",
            "Clear-Content -LiteralPath $p",
            "'x' | Out-File $p",
            "Copy-Item -LiteralPath $a -Destination $b -Force",
            "Copy-Item -LiteralPath $a -Destination $b",
            "[IO.File]::Copy($a, $b, $true)",
            "$w = [IO.StreamWriter]::new($p)",
            "$x | Tee-Object -FilePath $p",
            "$x | Export-Csv -Path $p",
            "Start-Process x -RedirectStandardOutput $p",
            "$s = [IO.File]::Open($p, 'Truncate')",
            "New-Item -ItemType File -Path $p -Force",
            "$stream.SetLength(0)",
            "$stream.WriteByte(0)",
            "$stream.Write($bytes, 0, $bytes.Length)",
            "sc -Path $p -Value x",
            "'x' > $p",
            "'x' >> $p",
        ]
        for line in must_hit:
            self.assertTrue(_line_writes(line), f"the scan missed: {line}")
        must_not_hit = [
            "$s = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)",
            "$s = [IO.File]::OpenRead($p)",
            "$ok = $a -ge $b",
            "if ($x -gt 2) { $y = 1 }",
            "Test-Path -LiteralPath $p",
            "Get-Item -LiteralPath $p",
            "$items = @($a -eq $b)",
            "$text = if ($null -eq $e) { '<none>' } else { $e }",
            "& git -C $RepoRoot cat-file -e $sha 2>$null",
            "New-Item -ItemType Directory -Path $d -Force | Out-Null",
        ]
        for line in must_not_hit:
            self.assertFalse(_line_writes(line), f"false positive: {line}")


class NoHardLinkInvariantWiringTests(unittest.TestCase):
    def test_the_attribution_job_never_builds_a_view_with_a_hard_link_and_always_pins_first(self) -> None:
        text = (BACHELOR / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        start = text.index("$OwnerClipDir = New-AttrCudaDirectory")
        body = text[start : text.index("Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews)\n    } catch {", start)]
        self.assertLess(body.index("Test-AttrCudaSymlinkCapability"), body.index("Open-AttrCudaReadOnlyHandle"))
        self.assertLess(body.index("Open-AttrCudaReadOnlyHandle"), body.index("New-AttrCudaOwnerFootageView"))
        self.assertLess(body.index("New-AttrCudaOwnerFootageView"), body.index("Test-AttrCudaFootagePart"))
        self.assertIn("-Mode $ownerViewMode", body)

    def test_the_view_is_only_ever_a_symlink_or_a_copy(self) -> None:
        text = (BACHELOR / "AttrCudaOwnerFootage.psm1").read_text(encoding="utf-8")
        self.assertIn("ValidateSet('symlink', 'copy')", text)
        self.assertNotIn("HardLink", "\n".join(_strip_hash_comments(text)))

    def test_the_owner_jobs_delete_only_through_the_identity_checked_primitive(self) -> None:
        for name in ("AttrCudaOwnerFootage.psm1", "Attr3FootageStageJob.psm1"):
            with self.subTest(source=name):
                code = "\n".join(_strip_hash_comments((BACHELOR / name).read_text(encoding="utf-8")))
                self.assertNotIn("Remove-Item", code)
                self.assertNotIn("Remove-AttrCudaPartialFile", code)
                self.assertNotIn("[IO.File]::Delete", code)
                self.assertIn("Remove-AttrCudaFileById", code)


# ---- 3. deletes on the paths that can hold an owner-footage name or a job copy of one ---------------

# Every source whose runtime can reach a view entry, a staged copy, a job scratch tree that holds one, or
# the share-side slot of one. (The build-route jobs -- assembler, stage, DLL-pair, fixture, smoke-runner --
# are guarded by test_owner_footage_delete_class.py instead: Remove-AttrCudaTree has NO journal-less mode any
# more, so they create their trees fresh (New-AttrCudaOwnedRoot) and delete only what that record proves.)
OWNER_CAPABLE_DELETE_SOURCES = (
    BACHELOR / "attr3-footage-stage.ps1",
    BACHELOR / "Attr3FootageStageJob.psm1",
    BACHELOR / "AttrCudaOwnerFootage.psm1",
    BACHELOR / "playback-attr-3-cuda-job.ps1",
)

RECURSIVE_DELETE_RE = re.compile(
    r"\b(?:Remove-Item|ri|rm|rmdir|del|erase|rd)\b[^\n|;]*?\s-(?:r|re|rec|recu|recur|recurs|recurse)\b"
    r"|\[(?:System\.)?IO\.Directory\]::Delete\([^)]*,\s*\$true\s*\)"
    r"|\bRemoveDirectory\w*\b.*\brecurs",
    re.IGNORECASE,
)
PATHNAME_PARTIAL_DELETE_RE = re.compile(r"\bRemove-AttrCudaPartialFile\b")
TREE_CALL_RE = re.compile(r"\bRemove-AttrCudaTree\b[^\n]*")


def _delete_violations(name: str, code_lines: list[str]) -> list[str]:
    found = []
    for number, line in enumerate(code_lines, start=1):
        if not line:
            continue
        if RECURSIVE_DELETE_RE.search(line):
            found.append(f"{name}:{number}: a recursive pathname delete: {line}")
        if PATHNAME_PARTIAL_DELETE_RE.search(line):
            found.append(f"{name}:{number}: Remove-AttrCudaPartialFile (a pathname delete) on an owner-capable path: {line}")
        for call in TREE_CALL_RE.findall(line):
            if call.strip().startswith("Remove-AttrCudaTree,") or "OwnedJournal" in call:
                continue
            if re.fullmatch(r"['\"]?Remove-AttrCudaTree['\"]?,?", line.strip()):
                continue  # a name in an embed list, not a call
            if re.match(r"Remove-AttrCudaTree\s*$", call.strip()) or "function" in line.lower():
                continue
            found.append(f"{name}:{number}: Remove-AttrCudaTree without -OwnedJournal (first-seen adoption): {line}")
    return found


class NoPathnameDeleteOnOwnerCapablePathsTests(unittest.TestCase):
    """OWNER-FOOTAGE-NO-HARDLINK-1 round 2 (hub ruling, sol r1 blockers 1 and 3): on every path that can
    hold an owner-footage name or a job copy of one there is no recursive pathname delete, no
    Remove-AttrCudaPartialFile, and every tree delete is JOURNALLED -- it removes only what the job
    recorded at creation. The behaviour is proven on real NTFS in test_owner_footage_creator_ownership.py;
    this keeps a later edit from putting a pathname delete back."""

    def test_no_owner_capable_source_deletes_by_pathname_or_recursively(self) -> None:
        violations = []
        for path in OWNER_CAPABLE_DELETE_SOURCES:
            self.assertTrue(path.is_file(), path)
            violations += _delete_violations(path.name, _strip_hash_comments(path.read_text(encoding="utf-8")))
        self.assertEqual(violations, [], "\n".join(violations))

    def test_the_owner_job_sweeps_its_work_tree_only_through_the_journal(self) -> None:
        code = "\n".join(_strip_hash_comments((BACHELOR / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")))
        # DUAL-VENUE-EVIDENCE-1: the trusted root is the venue's scratch-root TOKEN in the template; the generator expands it
        # to 'C:\mlvtmp' for the default (bachelor) venue, byte-identically (test_dual_venue_evidence.py pins that against master's
        # emitted job), and to the venue table's scratch root for another venue.
        self.assertIn("Remove-AttrCudaTree -TrustedRoot '__SCRATCH_ROOT__' -Path $Work -OwnedJournal $OwnerJournal", code)
        self.assertIn("Clear-AttrCudaOwnerFootageLeftovers -Directory (Join-Path $Work 'owner-clip') -Journal $OwnerJournal", code)
        # a left entry refuses the run with a typed, path-free result instead of carrying on into it
        self.assertIn("OWNER_WORK_NOT_CLEAN", code)

    def test_the_staging_cli_deletes_its_slots_only_by_the_identity_it_recorded_at_creation(self) -> None:
        code = "\n".join(_strip_hash_comments((BACHELOR / "attr3-footage-stage.ps1").read_text(encoding="utf-8")))
        self.assertIn("Remove-AttrCudaFileById -Path $createdSlot.Path -FileId $createdSlot.Id", code)
        self.assertIn("Id = $sendResult.Id", code)
        self.assertNotIn("createdSharePaths", code)

    def test_the_stage_job_never_adopts_a_first_seen_identity(self) -> None:
        text = (BACHELOR / "Attr3FootageStageJob.psm1").read_text(encoding="utf-8")
        start = text.index("$template = @'")
        template = "\n".join(_strip_hash_comments(text[start: text.index("\n'@", start)]))
        # the staged copy's identity comes from the spec; no Get-AttrCudaFileId -Path anywhere in the job
        self.assertIn("$rawPart.PSObject.Properties['stagedId']", template)
        self.assertNotIn("Get-AttrCudaFileId -Path", template)
        self.assertNotIn("seenStagedId", template)

    def test_the_leftover_sweep_deletes_only_journalled_entries(self) -> None:
        text = (BACHELOR / "AttrCudaOwnerFootage.psm1").read_text(encoding="utf-8")
        start = text.index("function Clear-AttrCudaOwnerFootageLeftovers")
        body = "\n".join(_strip_hash_comments(text[start: text.index("function Close-AttrCudaOwnerFootageWorkspace")]))
        self.assertNotIn("Get-AttrCudaFileId -Path $entry.FullName", body)
        self.assertIn("LEFT_LEGACY", body)
        self.assertIn("$records[", body)

    def test_the_scanner_catches_what_it_claims_to(self) -> None:
        must_hit = [
            "Remove-Item -LiteralPath $p -Recurse -Force",
            "Remove-Item $p -r -Force",
            "Remove-Item -Force -Recurse -LiteralPath $p",
            "rm -Recurse $p",
            "[IO.Directory]::Delete($p, $true)",
            "[void](Remove-AttrCudaPartialFile -TrustedRoot $r -Path $p)",
            "Remove-AttrCudaTree -TrustedRoot 'C:\\mlvtmp' -Path $Work",
            "$x = Remove-AttrCudaTree -TrustedRoot $a -Path $b",
        ]
        for line in must_hit:
            self.assertTrue(_delete_violations("probe", [line]), f"the scan missed: {line}")
        must_not_hit = [
            "Remove-Item -LiteralPath $emitPath -Force",
            "[IO.Directory]::Delete($shareStageDir, $false)",
            "Remove-AttrCudaTree -TrustedRoot 'C:\\mlvtmp' -Path $Work -OwnedJournal $OwnerJournal",
            "Remove-AttrCudaFileById -Path $p -FileId $id",
            "Remove-AttrCudaTree, `",
            "'Remove-AttrCudaTree',",
        ]
        for line in must_not_hit:
            self.assertEqual(_delete_violations("probe", [line]), [], f"false positive: {line}")


if __name__ == "__main__":
    unittest.main()
