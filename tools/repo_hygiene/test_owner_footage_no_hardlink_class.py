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
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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
# Code compiled or loaded at run time by the C# payload is outside the census, so each such route fails closed (r14 iter 1, A3 siblings). The PowerShell
# routes (Invoke-Expression, [scriptblock]::Create, Import-Module, an assembly load) are r15's AST census below; this text rule stays for the C# the AST cannot see.
_CS_DYNAMIC_CODE_RE = re.compile(r"Assembly\]?::Load\w*|\bLoad(?:From|File)?\s*\(|\bLoad(?:From|File)\b|CodeDom|CodeProvider|CodeAnalysis", re.IGNORECASE)
# A C0 control other than tab, CR and LF: str.splitlines() would split a line there while every line number here counts only LF (fable r14 finding 2).
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ---- r15 (sol r14 B1/B2, fable r14 HARDLINK-CENSUS-PS-DISPATCH-AST-1; folds LANE-DIRTY-SAVE-CENSUS-PS-AST-1): the PowerShell half is DEFAULT-DENY over the AST.
# r12-r14 closed the adversary's spellings one regex at a time, and every pass found more: whitespace or a backtick-newline after a member dot, a variable
# command, ForEach-Object -m, A`dd-Type. The real parser (ps_ast_inventory.ps1) has already resolved backticks, whitespace, quoting and escapes, so the census
# reads the names PowerShell binds and ALLOWS only what is pinned here: every command, member, static member, type, function and scriptblock variable the
# saver uses today. Anything computed (a member or command named by an expression, a variable or an expandable string), anything not pinned, and any name
# rebound (a function or alias over a pinned command, a function: or alias: path) is a violation, whatever its spelling. A new name in the saver therefore
# fails this test until it is pinned here, which is the review step. The text rules that stay are the ones the AST cannot see: the non-ASCII and control-
# character rules, the C# rules (escapes, natives, externs, reflection, run-time loading, link names: the payload is one string to the PowerShell parser) and
# the here-string payload checks (the payload the C# census reads is the one Add-Type compiles).
PS_AST_INVENTORY = ROOT / "tools" / "repo_hygiene" / "ps_ast_inventory.ps1"
# external commands (the saver's own functions are LANE_DIRTY_SAVE_PS_FUNCTIONS); each with a shape rule below where an argument could reach code
LANE_DIRTY_SAVE_PS_COMMANDS = {
    "Add-Type", "ForEach-Object", "Get-Command", "Get-Content", "Get-FileHash", "Get-Item", "git", "Join-Path", "New-Object", "Remove-Item",
    "Resolve-Path", "Set-Item", "Split-Path", "Test-Path", "Where-Object",
}
LANE_DIRTY_SAVE_PS_FUNCTIONS = {
    "Add-DirtyReuseHold", "ConvertTo-DirtySingleQuoted", "Format-DirtyResolved", "Get-DirtyDirIdentity", "Get-DirtyHeldIdentity", "Get-DirtyKernelPath",
    "Get-DirtyReapplyCommand", "Get-DirtyRegularDigest", "Get-DirtyRepoSample", "Invoke-DirtyDestCheckpoint", "Invoke-DirtyFileMove", "Invoke-DirtyGitCapture",
    "Lock-DirtyDirectory", "Lock-DirtyFile", "Move-DirtyEvidenceToFreeName", "New-DirtyHeldTemp", "Read-DirtyEvidence", "Remove-DirtyTemp",
    "Resolve-DirtyRealPath", "Save-DirtyWorktreeEvidence", "Save-DirtyWorktreeEvidenceInGate", "Test-DirtyDestStable", "Test-DirtyDiffHasGitlink",
    "Test-DirtyEvidenceUnchanged", "Test-DirtyPatchComplete", "Test-DirtyPathInside", "Test-DirtyTempHeld", "Test-DirtyUncLocalHost", "Test-DirtyUnderIdentity",
    "Unlock-DirtyDirectory", "Unlock-DirtyTemp", "Write-DirtyTempBytes", "Write-DirtyTempLines",
}
# `& $name` is allowed only for these, and only while every value they can hold is a scriptblock literal (rules in _ps_ast_violations)
LANE_DIRTY_SAVE_PS_CALL_VARIABLES = {"Guard", "isUnc", "placeFailed", "repoCheck"}
LANE_DIRTY_SAVE_PS_MEMBERS = {
    "Absent", "Add", "Address", "ArgumentList", "Attributes", "BaseStream", "Bytes", "CommonDir", "CommonId", "ComputeHash", "ContainsKey", "CopyTo", "Count",
    "CreateNoWindow", "Dir", "Dispose", "EndsWith", "Exception", "Exit", "ExitCode", "FileName", "FullName", "GetBytes", "GetIPProperties", "GitId", "Groups",
    "Handle", "Hash", "HResult", "Id", "InnerException", "IPAddressToString", "Keys", "Length", "LinkTarget", "LinkType", "Message", "Moved", "Path", "Placed",
    "ProviderPath", "ReadLine", "ReadToEndAsync", "Reason", "RedirectStandardError", "RedirectStandardOutput", "ResolveLinkTarget", "Source", "Split",
    "StandardError", "StandardOutput", "StartsWith", "Substring", "Success", "ToArray", "ToLowerInvariant", "ToString", "Trim", "TrimEnd", "UnicastAddresses",
    "UseShellExecute", "Value", "WaitForExit",
}
LANE_DIRTY_SAVE_PS_STATIC_MEMBERS = {
    "BitConverter::ToString", "byte[]::new", "Console::OutputEncoding", "DirtyFinalPath::CreateHeld", "DirtyFinalPath::CreateHeldIn",
    "DirtyFinalPath::DeleteIfIdentity", "DirtyFinalPath::DeleteIfIdentityIn", "DirtyFinalPath::FileIdentity", "DirtyFinalPath::HandleFileIdentity",
    "DirtyFinalPath::HandleIdentity", "DirtyFinalPath::HandleIsReparse", "DirtyFinalPath::Identity", "DirtyFinalPath::MoveIn", "DirtyFinalPath::Pin",
    "DirtyFinalPath::PinFile", "DirtyFinalPath::RegularFileDigest", "DirtyFinalPath::RegularFileDigestHeld", "DirtyFinalPath::Resolve", "DirtyFinalPath::Unpin",
    "DirtyFinalPath::WriteHeld", "Environment::NewLine", "Guid::NewGuid", "IntPtr::Zero", "regex::Match", "regex::Replace", "string::Intern",
    "System.Collections.Generic.List[object]::new", "System.ComponentModel.Win32Exception::new", "System.Diagnostics.Process::Start",
    "System.InvalidOperationException::new", "System.IO.Directory::GetFileSystemEntries", "System.IO.File::Move", "System.IO.File::ReadAllBytes",
    "System.IO.File::ReadAllText", "System.IO.FileAttributes::ReparsePoint", "System.IO.IOException::new", "System.IO.MemoryStream::new",
    "System.IO.Path::Combine", "System.IO.Path::DirectorySeparatorChar", "System.IO.Path::GetDirectoryName", "System.IO.Path::GetFullPath",
    "System.IO.Path::GetPathRoot", "System.IO.StreamReader::new", "System.Net.Dns::GetHostAddresses",
    "System.Net.NetworkInformation.NetworkInterface::GetAllNetworkInterfaces", "System.Runtime.InteropServices.COMException::new",
    "System.Security.Cryptography.SHA256::Create", "System.StringComparison::Ordinal", "System.StringComparison::OrdinalIgnoreCase",
    "System.StringSplitOptions::RemoveEmptyEntries", "System.Threading.Monitor::Exit", "System.Threading.Monitor::IsEntered", "System.Threading.Monitor::TryEnter",
}
LANE_DIRTY_SAVE_PS_TYPES = {
    "BitConverter", "bool", "byte[]", "char", "char[]", "Console", "DirtyFinalPath", "Environment", "Guid", "hashtable", "IntPtr", "long", "ref", "regex",
    "scriptblock", "string", "string[]", "System.Collections.Generic.List[object]", "System.ComponentModel.Win32Exception", "System.Diagnostics.Process",
    "System.InvalidOperationException", "System.IO.Directory", "System.IO.DirectoryInfo", "System.IO.File", "System.IO.FileAttributes", "System.IO.IOException",
    "System.IO.MemoryStream", "System.IO.Path", "System.IO.StreamReader", "System.Management.Automation.MethodInvocationException", "System.Net.Dns",
    "System.Net.NetworkInformation.NetworkInterface", "System.Runtime.InteropServices.COMException", "System.Security.Cryptography.SHA256",
    "System.StringComparison", "System.StringSplitOptions", "System.Threading.Monitor", "type", "void",
}
# each New-Object by its whole text: a constructor argument can set what a launch runs (ProcessStartInfo('pwsh'), r15 iter 1)
LANE_DIRTY_SAVE_PS_NEW_OBJECTS = {"New-Object System.Diagnostics.ProcessStartInfo", "New-Object System.IO.MemoryStream", "New-Object System.Text.UTF8Encoding ($false)"}
# r15 iter 1 (adversary 99ae3557-A1/A2): a child process runs code no census here reads ([Process]::Start('pwsh', ...), git -c alias.x=!cmd, an env
# var git executes such as GIT_EXTERNAL_DIFF). So every launch is pinned EXACTLY: each git, git-capture and env-write call by its whole argument text,
# every write to a variable that reaches one (assignment, foreach source, parameter type, method call), the one Process::Start, and no process start in
# the C# payload. A changed git argument, env name or launch variable fails until it is re-pinned here, which is the review step.
LANE_DIRTY_SAVE_PS_LAUNCHES = {
    "git -C $Wt rev-parse --path-format=absolute --absolute-git-dir --git-common-dir",
    "git -C $Wt rev-parse --show-prefix",
    "git -C $Wt rev-parse --show-toplevel",
    "git -C $Wt diff $side --raw --no-renames --no-ext-diff --ignore-submodules=none",
    "git -C $Wt -c core.quotepath=false status --porcelain --untracked-files=all --ignore-submodules=none",
    "git -C $Wt -c core.quotepath=false ls-files -u",
    "git -C $Wt rev-parse -q --verify $ref",
    "git -C $Wt diff --cached --quiet --no-ext-diff --ignore-submodules=none",
    "git -C $Wt apply --cached --check -R --whitespace=nowarn $stagedTmp.Path",
    "git -C $Wt apply --check -R --whitespace=nowarn $patch",
    "Invoke-DirtyGitCapture -Wt $Wt -GitArgs (@('diff', 'HEAD') + $shape)",
    "Invoke-DirtyGitCapture -Wt $Wt -GitArgs (@('diff', '--cached') + $shape)",
    "Test-DirtyPatchComplete -Wt $Wt -Full $full -GitDiffArgs (@('diff', 'HEAD') + $shape)",
    "Test-DirtyPatchComplete -Wt $Wt -Full $stagedFull -GitDiffArgs (@('diff', '--cached') + $shape)",
   "Remove-Item -LiteralPath \"Env:$v\" -ErrorAction SilentlyContinue",
    "Set-Item -LiteralPath \"Env:$v\" -Value $savedEnv[$v]",
}
_PS_LAUNCH_COMMANDS = {"git", "invoke-dirtygitcapture", "test-dirtypatchcomplete", "set-item", "remove-item"}
LANE_DIRTY_SAVE_PS_LAUNCH_VARIABLES = {
    "Wt", "side", "ref", "patch", "stagedTmp", "shape", "savedEnv", "gitEnv", "gitDir", "psi", "a", "GitArgs", "GitDiffArgs", "v",
}
LANE_DIRTY_SAVE_PS_LAUNCH_WRITES = {
    # assignments (left = right), including every env: write
    "$psi = New-Object System.Diagnostics.ProcessStartInfo",
    "$psi.FileName = @(Get-Command git -CommandType Application)[0].Source",
    "$psi.UseShellExecute = $false", "$psi.RedirectStandardOutput = $true", "$psi.RedirectStandardError = $true", "$psi.CreateNoWindow = $true",
    "$stagedTmp = $null",
    "$gitEnv = 'GIT_DIFF_OPTS', 'GIT_EXTERNAL_DIFF', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'",
    "$savedEnv = @{}",
    "$savedEnv[$v] = $found.Value",
    "$Wt = (Resolve-Path -LiteralPath $Wt).ProviderPath",
    "$Wt = $wtReal",
    "$gitDir = @(& git -C $Wt rev-parse --path-format=absolute --absolute-git-dir --git-common-dir 2>$null)",
    "$shape = @('--binary', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--submodule=short', '--ignore-submodules=none', '-U3', "
    "'--src-prefix=a/', '--dst-prefix=b/')",
    "$patch = $placed.Path",
    "$stagedTmp = New-DirtyHeldTemp $Dest ('dirty-worktree-staging-' + [Guid]::NewGuid().ToString('N') + '.tmp') $destDir",
    "$env:GIT_DIR = $gitDir[0]", "$env:GIT_COMMON_DIR = $gitDir[1]", "$env:GIT_WORK_TREE = $Wt",
    # foreach sources (name in collection)
    "foreach $a in (@('-C', $Wt) + $GitArgs)", "foreach $a in $target", "foreach $a in (@('-C', $Wt) + $GitDiffArgs)", "foreach $v in $gitEnv",
    "foreach $side in 'HEAD', '--cached'", "foreach $ref in 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'",
    "foreach $v in 'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE'", "foreach $v in $savedEnv.Keys",
    # parameters (name: types) and method calls on a launch variable
    "param $Wt: string", "param $GitArgs: string[]", "param $GitDiffArgs: string[]", "param $GitDir: string", "param $Patch: string",
    "call $a.StartsWith('127.')",
    "[System.Diagnostics.Process]::Start($psi)",
}
# r15 iter 2 (adversary 39a58424-A1): the pins above follow NAMES, so an alias ($x = $psi; $x.FileName = 'pwsh') or a call through a member chain
# ($psi.ArgumentList.Add('-c')) changed the launch object unseen. Every reference to a mutable launch object BY ITS NAME (any scope qualifier, $variable:
# included since r16) is therefore pinned by its site (the enclosing assignment, else the command, expression, parameter or foreach header;
# ps_ast_inventory.ps1 Get-UseSite), so a new alias or call is a new site. A reference by ANOTHER name (a provider path, SessionState) is the r16 rule below.
LANE_DIRTY_SAVE_PS_LAUNCH_OBJECTS = {"psi", "savedEnv", "gitEnv", "shape", "stagedTmp", "gitDir", "GitArgs", "GitDiffArgs"}
LANE_DIRTY_SAVE_PS_LAUNCH_OBJECT_USES = {
    "gitargs @ foreach ($a in (@('-C', $Wt) + $GitArgs))", "gitargs @ param [string[]]$GitArgs",
    "gitdiffargs @ foreach ($a in (@('-C', $Wt) + $GitDiffArgs))", "gitdiffargs @ param [string[]]$GitDiffArgs",
    "gitdir @ $cdFile = Join-Path $GitDir 'commondir'", "gitdir @ $common = $GitDir",
    "gitdir @ $common = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($GitDir, $text))", "gitdir @ $p = Join-Path $GitDir $n",
    "gitdir @ return @{ GitId = (Get-DirtyDirIdentity $GitDir); CommonDir = $common; CommonId = (Get-DirtyDirIdentity $common); Absent = $absent }", "gitdir @ param [string]$GitDir",
    "gitdir @ $env:GIT_COMMON_DIR = $gitDir[1]", "gitdir @ $env:GIT_DIR = $gitDir[0]", "gitdir @ $gitDir | ForEach-Object { Get-DirtyDirIdentity $_ }",
    "gitdir @ $gitDir = @(& git -C $Wt rev-parse --path-format=absolute --absolute-git-dir --git-common-dir 2>$null)",
    "gitdir @ $h = Lock-DirtyDirectory $gitDir[$i]", "gitdir @ $i -lt $gitDir.Count",
    "gitdir @ $LASTEXITCODE -ne 0 -or $again.Count -ne 2 -or $again[0] -cne $gitDir[0] -or $again[1] -cne $gitDir[1]",
    "gitdir @ $LASTEXITCODE -ne 0 -or $gitDir.Count -ne 2", "gitdir @ (Get-DirtyDirIdentity $gitDir[$i]) -cne $gitDirIds[$i]", "gitdir @ (Join-Path $Wt '.git'), (Join-Path $gitDir[0] 'commondir')",
    "gitenv @ foreach ($v in $gitEnv)",
    "gitenv @ $gitEnv = 'GIT_DIFF_OPTS', 'GIT_EXTERNAL_DIFF', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'",
    "psi @ $p = [System.Diagnostics.Process]::Start($psi)", "psi @ $psi = New-Object System.Diagnostics.ProcessStartInfo",
    "psi @ $psi.CreateNoWindow = $true", "psi @ $psi.FileName = @(Get-Command git -CommandType Application)[0].Source",
    "psi @ $psi.RedirectStandardError = $true", "psi @ $psi.RedirectStandardOutput = $true", "psi @ $psi.UseShellExecute = $false",
    "psi @ [void]$psi.ArgumentList.Add($a)",
    "savedenv @ $savedEnv = @{}", "savedenv @ foreach ($v in $savedEnv.Keys)", "savedenv @ $savedEnv[$v] = $found.Value",
    "savedenv @ Set-Item -LiteralPath \"Env:$v\" -Value $savedEnv[$v]",
    "shape @ $cap = Invoke-DirtyGitCapture -Wt $Wt -GitArgs (@('diff', 'HEAD') + $shape)",
    "shape @ $shape = @('--binary', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--submodule=short', '--ignore-submodules=none', '-U3', "
    "'--src-prefix=a/', '--dst-prefix=b/')",
    "shape @ $stagedCap = Invoke-DirtyGitCapture -Wt $Wt -GitArgs (@('diff', '--cached') + $shape)",
    "shape @ -not (Test-DirtyPatchComplete -Wt $Wt -Full $stagedFull -GitDiffArgs (@('diff', '--cached') + $shape))",
    "shape @ -not (Test-DirtyPatchComplete -Wt $Wt -Full $full -GitDiffArgs (@('diff', 'HEAD') + $shape))",
    "stagedtmp @ $stagedFull = Write-DirtyTempBytes $stagedTmp $stagedCap.Bytes",
    "stagedtmp @ $stagedPlaced = Move-DirtyEvidenceToFreeName -Tmp $stagedTmp.Path -Dir $Dest -Stem 'dirty-worktree-staged-' -Ext '.diff' -Full $stagedFull "
    "-Guard $guard -Hold $stagedTmp -DirHandle $destDir",
    "stagedtmp @ $stagedTmp = $null",
    "stagedtmp @ $stagedTmp = New-DirtyHeldTemp $Dest ('dirty-worktree-staging-' + [Guid]::NewGuid().ToString('N') + '.tmp') $destDir",
    "stagedtmp @ foreach ($t in $tmp, $stagedTmp, $statTmp, $mergeTmp)", "stagedtmp @ $why = $stagedTmp.Reason", "stagedtmp @ $why = Test-DirtyTempHeld $stagedTmp",
    "stagedtmp @ & git -C $Wt apply --cached --check -R --whitespace=nowarn $stagedTmp.Path 2>$null", "stagedtmp @ -not $stagedTmp.Path",
    "stagedtmp @ -not (Test-DirtyEvidenceUnchanged -Path $stagedTmp.Path -Full $stagedFull)",
}
# r16 (sol r15b B1): every pin above follows a NAME, and PowerShell reaches a binding by other names too: (Get-Item Variable:psi).Value IS $psi, and
# Set-Item Variable:isUnc rebinds a call variable. The whole provider / session-state binding class is therefore DEFAULT-DENY, whatever the variable:
# - every *-Item, *-ItemProperty, *-ChildItem, *-Content and *-Variable call (any alias, any module qualifier) must be one of these exact texts, alone in its
#   pipeline (so no path, literal or dynamic, arrives by an argument or by pipeline input that is not pinned here);
# - no string literal anywhere may name the Variable:, Function: or Alias: drive (any case, any quoting, provider-qualified or not);
# - no variable may be written with a variable:, function: or alias: qualifier ($variable:psi, ${function:x}, $alias:x);
# - $ExecutionContext, $PSCmdlet and $PSDefaultParameterValues may not be used at all (the last rebinds a pinned cmdlet's parameters; fable r15 hardening), and
#   no member may be SessionState, SessionStateProxy, PSVariable, InvokeProvider, InvokeCommand or Runspace (how a PSVariable is reached from code);
# - what a pinned provider call returns ($item, $found, $first) is used only at the sites pinned below.
# NOT pinned (the census's guarantee stops here): the string VALUE a pinned call's variable path ($Path, $dotGit) holds at run time. A path built from pieces
# could name a binding there, and the pinned uses of its result only read LinkType / Attributes / Count / the first line, so nothing can be written through it.
# Nor are writes to preference variables ($ErrorActionPreference and the like): they change how an error is reported, never which object a name binds.
# WHAT THIS CENSUS PROVES, as a whole: the saver's PowerShell source names only the pinned commands, functions, members, types and scriptblock call variables;
# every child process it can start, with its argument text and every write that reaches it, is one of the pinned ones; no pinned binding is reached by another
# name; the one Add-Type compiles exactly the checked payload, and the payload's one NtSetInformationFile call uses class 10. It reads SOURCE: what git does
# with repository data (config, hooks, attributes) and every run-time race are the saver's own guards, named in its header, not this census.
LANE_DIRTY_SAVE_PS_PROVIDER_CALLS = {
    "Get-Item -LiteralPath $Path -Force -ErrorAction Stop",
    "Get-Item -LiteralPath \"Env:$v\" -ErrorAction SilentlyContinue",
    "Get-Content -LiteralPath $dotGit -TotalCount 1 -ErrorAction Stop",
    "Remove-Item -LiteralPath \"Env:$v\" -ErrorAction SilentlyContinue",
    "Set-Item -LiteralPath \"Env:$v\" -Value $savedEnv[$v]",
}
_PS_PROVIDER_FAMILY_RE = re.compile(r"-(?:item|itemproperty|itempropertyvalue|childitem|content|variable)$", re.IGNORECASE)
_PS_PROVIDER_ALIASES = {
    "ac", "cat", "clc", "cli", "clp", "clv", "copy", "cp", "cpi", "cpp", "del", "dir", "erase", "gc", "gci", "gi", "gp", "gpv", "gv", "ii", "ls", "mi",
    "move", "mp", "mv", "ni", "nv", "rd", "ren", "ri", "rm", "rmdir", "rni", "rnp", "rp", "rv", "sc", "set", "si", "sp", "sv", "type",
}
_PS_BINDING_DRIVE_RE = re.compile(r"(?:^|[\s'\"(,;|\\/}])(?:[\w.]*\\)?(variable|function|alias)::?", re.IGNORECASE)
_PS_BINDING_QUALIFIERS = {"variable", "function", "alias"}
_PS_SESSION_VARIABLES = {"executioncontext", "pscmdlet", "psdefaultparametervalues"}
_PS_SESSION_MEMBERS = {"sessionstate", "sessionstateproxy", "psvariable", "invokeprovider", "invokecommand", "runspace"}
LANE_DIRTY_SAVE_PS_PROVIDER_RESULTS = {"item", "found", "first"}
LANE_DIRTY_SAVE_PS_PROVIDER_RESULT_USES = {
    "item @ $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop",
    "item @ $item.LinkType -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)",
    "item @ $item.Attributes -band [System.IO.FileAttributes]::ReparsePoint",
    "found @ $found = Get-Item -LiteralPath \"Env:$v\" -ErrorAction SilentlyContinue", "found @ $null -eq $found", "found @ $savedEnv[$v] = $found.Value",
    "first @ $first = @(Get-Content -LiteralPath $dotGit -TotalCount 1 -ErrorAction Stop)",
    "first @ $m = if ($first.Count -eq 1) { [regex]::Match($first[0], '^gitdir: (.+?)\\s*$') } else { $null }",
    "first @ [regex]::Match($first[0], '^gitdir: (.+?)\\s*$')",
}
_PS_USE_NAMES = sorted(LANE_DIRTY_SAVE_PS_LAUNCH_OBJECTS | LANE_DIRTY_SAVE_PS_PROVIDER_RESULTS | {"ExecutionContext", "PSCmdlet", "PSDefaultParameterValues"})
# a process started, or code reached at run time, from inside the C# payload (the AST cannot see the payload; payload-only so the PowerShell lines that
# legitimately name ProcessStartInfo do not count)
_PAYLOAD_PROCESS_RE = re.compile(
    r"\bProcess\b|ProcessStartInfo|ShellExecute|CreateProcess|WinExec|\bActivator\b|\bdynamic\b|\bGetType\s*\(|SetEnvironmentVariable"
    # r15 iter 2 (adversary 39a58424-A2): PowerShell itself is a default Add-Type reference, so its scripting API runs any script from C#
    r"|\bPowerShell\b|ScriptBlock|Runspace|AddScript|AddCommand|InvokeScript|InvokeCommand|System\.Management|\bCompile\s*\(|\bAppDomain\b|\bAssembly\b|Emit\b"
    # r16 (adversary a11c4459-A2): Microsoft.VisualBasic.Interaction.Shell starts a process; COM activation by ProgID / CLSID runs any registered server
    r"|\bShell\b|Interaction|VisualBasic|GetTypeFrom\w*|\bglobal::|\bnamespace\b|\bextern\s+alias\b"
    # r16 iter 2 (adversary 31a40a36-A2/A3): a block comment can sit inside a qualified name (System/**/.IO.File); a library can be chosen or loaded by path
    r"|/\*|LoadLibrary|SetDllDirectory|AddDllDirectory|DllImportSearchPath|DefaultDllImportSearchPaths")  # C# is case-sensitive: a case variant does not compile
# r16 iter 2 (adversary 31a40a36-A3): an extern's NAME is pinned, so its LIBRARY must be too: a DllImport of another DLL runs that DLL's DllMain on the first
# call, before any export is resolved. Every DllImport attribute is one of these texts, and there is one per extern.
LANE_DIRTY_SAVE_PAYLOAD_DLLIMPORTS = {
    '[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]', '[DllImport("kernel32.dll", SetLastError = true)]',
    '[DllImport("kernel32.dll")]', '[DllImport("ntdll.dll")]',
}
_PAYLOAD_DLLIMPORT_RE = re.compile(r"DllImport")
_PAYLOAD_DLLIMPORT_ATTRIBUTE_RE = re.compile(r"\[DllImport\([^\]\n]*\)\]")
# r16 (adversary a11c4459-A2), default-deny: the payload's using directives are exactly these, and every name it qualifies from System. or Microsoft. is
# one of these, so a library outside them (Microsoft.VisualBasic, System.Diagnostics, System.Net, ...) cannot be reached by its full name or an alias
LANE_DIRTY_SAVE_PAYLOAD_USINGS = ["using System;", "using System.Runtime.InteropServices;", "using System.Text;"]
LANE_DIRTY_SAVE_PAYLOAD_QUALIFIED = {
    "Microsoft.Win32.SafeHandles.SafeFileHandle", "System.IO.FileAccess.Read", "System.IO.FileAccess.Write", "System.IO.FileStream",
    "System.IO.Path.GetFileName", "System.IO.Path.GetFullPath", "System.Runtime.InteropServices", "System.Security.Cryptography.SHA256",
    "System.Security.Cryptography.SHA256.Create", "System.Text",
}
# a line comment inside the name counts (iter 2). r17 (CodeQL check-run 123193275334, inefficient regular expression): a comment runs to its line end and
# takes it, so each separator run has exactly one parse; r16's `//[^\n]*` could stop anywhere, so a run of '//' split many ways inside the outer '+'.
# r17 iter 1 (adversary 4b76fdba-A1): a C# line ends at CR, LF, NEL, LS or PS, so a comment ends at any of them (a CR-only line end hid the name)
_CS_LINE_END = "\r\n\x85\u2028\u2029"
_CS_LINE_COMMENT = f"//[^{_CS_LINE_END}]*"
_PAYLOAD_QUALIFIED_RE = re.compile(rf"\b(?:System|Microsoft)(?:(?:\s|{_CS_LINE_COMMENT}[{_CS_LINE_END}])*\.(?:\s|{_CS_LINE_COMMENT}[{_CS_LINE_END}])*\w+)+")
# r17 iter 1 (adversary 4b76fdba-A2): a '#' line is a C# preprocessor directive, which can sit between the parts of a qualified name (System, #if true, #endif,
# .IO.File) or choose the text that compiles. The payload has none, so any '#' in it is denied rather than modelled as a separator.
_PAYLOAD_DIRECTIVE_RE = re.compile(r"#")
# the r16 pattern, kept only as the reference the r17 rewrite is compared with; it is exponential, so it is only ever run in a child under a timeout
_PAYLOAD_QUALIFIED_R16_PATTERN = r"\b(?:System|Microsoft)(?:(?:\s|//[^\n]*)*\.(?:\s|//[^\n]*)*\w+)+"
_FINDITER_CHILD = (
    "import json, re, sys, time\n"
    "request = json.load(sys.stdin)\n"
    "started = time.perf_counter()\n"
    "found = [[[m.start(), m.end(), m.group(0)] for m in re.finditer(request['pattern'], text)] for text in request['texts']]\n"
    "print(json.dumps({'found': found, 'seconds': time.perf_counter() - started}))\n")
_PAYLOAD_USING_RE = re.compile(r"^\s*using\b(?!\s*\()[^\n]*", re.MULTILINE)
# common parameters that write a variable by NAME (-OutVariable guard would put a pipeline's output into a call variable)
_PS_VARIABLE_WRITING_PARAMETERS = ("outvariable", "errorvariable", "warningvariable", "informationvariable", "pipelinevariable")
_PS_VARIABLE_WRITING_ALIASES = {"ov", "ev", "wv", "iv", "pv"}
_PS_INVENTORY_TIMEOUT = 600
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


def _finditer_in_child(pattern: str, texts: list[str], timeout: float) -> dict:
    """{'found': [[start, end, text] per match] per text, 'seconds': search time} for `pattern`, searched in a child so a pattern that backtracks
    exponentially is killed at `timeout` instead of wedging the suite (r17)."""
    proc = subprocess.run([sys.executable, "-I", "-c", _FINDITER_CHILD], input=json.dumps({"pattern": pattern, "texts": texts}).encode("ascii"),
                          capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"the finditer child exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-500:]}")
    return json.loads(proc.stdout)


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
        line = text.split("\n")[number - 1].strip()
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


def _ps_inventories(texts: list[str]) -> list[dict] | str:
    """One inventory per text from the real PowerShell parser (ps_ast_inventory.ps1), or why none could be read; the census then fails closed."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        return "pwsh is not on PATH"
    try:
        proc = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(PS_AST_INVENTORY), "-UseNames", ",".join(_PS_USE_NAMES)],
            input=json.dumps(texts).encode("ascii"), capture_output=True, timeout=_PS_INVENTORY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"the parser did not run: {type(error).__name__}: {error}"
    if proc.returncode != 0:
        return f"the parser exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
    try:
        inventories = json.loads(proc.stdout.decode("ascii"))
    except ValueError as error:
        return f"the parser's output is not ASCII JSON: {error}"
    if not isinstance(inventories, list) or len(inventories) != len(texts):
        return f"the parser returned {len(inventories) if isinstance(inventories, list) else 'no'} inventories for {len(texts)} text(s)"
    return inventories


def _ps_member_name_violation(member: dict) -> str | None:
    """sol r14 B1: a member named by an expression, a variable or a string built at run time ([T]. ('Get'+'Method'), with any whitespace or a backtick-newline
    after the dot) can reach any method, so the census cannot bound it. The parser reports the member node, not its spelling."""
    if member["name"] is None:
        return f"{member['line']}: a member named at run time ({member['kind']})"
    return None


def _ps_ast_violations(inv: dict, payload_bodies: list[str]) -> list[str]:
    """The default-deny census of one saver text's PowerShell AST inventory (see the r15 block above LANE_DIRTY_SAVE_PS_COMMANDS)."""
    def lowered(names) -> set[str]:
        return {name.lower() for name in names}

    commands, functions = lowered(LANE_DIRTY_SAVE_PS_COMMANDS), lowered(LANE_DIRTY_SAVE_PS_FUNCTIONS)
    call_variables, members = lowered(LANE_DIRTY_SAVE_PS_CALL_VARIABLES), lowered(LANE_DIRTY_SAVE_PS_MEMBERS)
    launch_variables = lowered(LANE_DIRTY_SAVE_PS_LAUNCH_VARIABLES)
    static_members, types = lowered(LANE_DIRTY_SAVE_PS_STATIC_MEMBERS), lowered(LANE_DIRTY_SAVE_PS_TYPES)
    violations = [f"{error['line']}: the PowerShell parser rejects the text: {error['message']}" for error in inv["errors"]]
    violations += [f"{using['line']}: a using statement loads code the census cannot see: {using['text']}" for using in inv["using"]]
    violations += [f"#Requires loads {count} {kind}" for kind, count in inv["requires"].items() if count]
    violations += [f"{other['line']}: a {other['kind']} defines code outside the census" for other in inv["other"]]
    defined: set[str] = set()
    for function in inv["functions"]:
        name = function["name"].lower()
        if name not in functions:
            violations.append(f"{function['line']}: a function outside the pinned set (it can rebind a command): {function['name']}")
        elif name in defined:
            violations.append(f"{function['line']}: a function defined a second time (the later body rebinds the name): {function['name']}")
        defined.add(name)
    add_types = []
    for command in inv["commands"]:
        line, name, elements = command["line"], command["name"], command["elements"]
        if name is None:
            static_block = command["head"] == "ScriptBlockExpressionAst" and command["operator"] in ("Ampersand", "Dot")
            call_variable = command["operator"] == "Ampersand" and (command["head_var"] or "").lower() in call_variables
            if not (static_block or call_variable):
                violations.append(f"{line}: a command named at run time ({command['head']}): {command['text']}")
            continue
        key = name.lower()
        if key == "add-type" or key.endswith("\\add-type"):
            add_types.append(command)
        if key not in commands and key not in functions:
            violations.append(f"{line}: a command outside the pinned set: {name}")
            continue
        for element in elements:
            parameter = (element["param"] or "").lower()
            if element["splatted"]:
                violations.append(f"{line}: {name} is splatted (its parameters are chosen at run time)")
            if parameter and (parameter in _PS_VARIABLE_WRITING_ALIASES or any(full.startswith(parameter) for full in _PS_VARIABLE_WRITING_PARAMETERS)):
                violations.append(f"{line}: {name} -{element['param']} writes a variable by name")
            if element["kind"] == "StringConstantExpressionAst" and (element["value"] or "").lower() in call_variables:
                violations.append(f"{line}: {name} names the call variable {element['value']!r}")
        kinds = [element["kind"] for element in elements]
        if key in ("foreach-object", "where-object") and (not kinds or set(kinds) != {"ScriptBlockExpressionAst"}):
            violations.append(f"{line}: {name} with anything but scriptblocks (a member name, -MemberName or any prefix of it invokes a method by name)")
        elif key == "get-command" and [(e["kind"], e["param"], e["value"], e["param_arg"]) for e in elements] != [
                ("StringConstantExpressionAst", None, "git", None), ("CommandParameterAst", "CommandType", None, None),
                ("StringConstantExpressionAst", None, "Application", None)]:
            violations.append(f"{line}: Get-Command other than 'Get-Command git -CommandType Application'")
        elif key == "new-object" and " ".join([name] + [element["text"] for element in elements]) not in LANE_DIRTY_SAVE_PS_NEW_OBJECTS:
            violations.append(f"{line}: New-Object of a type or with arguments outside {sorted(LANE_DIRTY_SAVE_PS_NEW_OBJECTS)}")
        elif key in _PS_LAUNCH_COMMANDS:
            launch = " ".join([name] + [element["text"] for element in elements])
            if launch not in LANE_DIRTY_SAVE_PS_LAUNCHES:
                violations.append(f"{line}: a process launch or env write outside the pinned set (a function: path or a git -c alias reaches code): {launch}")
    if len(add_types) != 1:
        violations.append(f"Add-Type must be invoked exactly once, found lines {[command['line'] for command in add_types]}")
    else:
        site = add_types[0]
        shape = [(e["kind"], e["param"], e["param_arg"], e["string_type"]) for e in site["elements"]]
        body = site["elements"][-1]["value"] if site["elements"] else None
        if (site["text"] != "Add-Type" or site["operator"] != "Unknown" or site["pipeline"] != 1
                or shape != [("CommandParameterAst", "TypeDefinition", None, None), ("StringConstantExpressionAst", None, None, "SingleQuotedHereString")]):
            violations.append(f"{site['line']}: the one Add-Type must be exactly {LANE_DIRTY_SAVE_ADD_TYPE_LINE!r} with its here-string, alone in its pipeline")
        elif len(payload_bodies) != 1 or body.replace("\r\n", "\n") != payload_bodies[0].replace("\r\n", "\n"):
            violations.append(f"{site['line']}: the text Add-Type compiles is not the payload the C# census read")
    for member in inv["members"]:
        named = _ps_member_name_violation(member)
        if named:
            violations.append(named)
        if member["name"] is None:
            continue  # a computed name is the rule above; the pinned sets below hold literal names only
        if (member["invoke"] and (member["on_var"] or "").lower() in launch_variables and f"call {member['text']}" not in LANE_DIRTY_SAVE_PS_LAUNCH_WRITES) or (
                f"{member['on']}::{member['name']}".lower() == "system.diagnostics.process::start" and member["text"] not in LANE_DIRTY_SAVE_PS_LAUNCH_WRITES):
            violations.append(f"{member['line']}: a launch variable or Process::Start used outside the pinned set: {member['text']}")
        if member["static"]:
            if member["on"] is None:
                violations.append(f"{member['line']}: a static member read from a type chosen at run time: ::{member['name']}")
            elif f"{member['on']}::{member['name']}".lower() not in static_members:
                violations.append(f"{member['line']}: a static member outside the pinned set: [{member['on']}]::{member['name']}")
        elif member["name"].lower() not in members:
            violations.append(f"{member['line']}: a member outside the pinned set: .{member['name']}")
    violations += [f"{t['line']}: a type outside the pinned set ({t['where']}): [{t['name']}]" for t in inv["types"] if t["name"].lower() not in types]
    violations += [f"{v['line']}: a ${{{v['path']}}} reference (only env: is allowed; function: and alias: rebind commands)"
                   for v in inv["drive_vars"] if (v["drive"] or "").lower() != "env"]
    for assignment in inv["assignments"]:
        if any(name.lower() in call_variables for name in assignment["names"]) and not (
                len(assignment["names"]) == 1 and (assignment["name"] or "").lower() in call_variables
                and assignment["operator"] == "Equals" and assignment["right"] == "ScriptBlockExpressionAst"):
            violations.append(f"{assignment['line']}: a call variable is given something other than a scriptblock literal ({assignment['right']})")
    for parameter in inv["params"]:
        if parameter["name"].lower() in call_variables and [t.lower() for t in parameter["types"]] != ["scriptblock"]:
            violations.append(f"{parameter['line']}: the call variable parameter ${parameter['name']} is not typed [scriptblock]")
    violations += [f"{row['line']}: the call variable ${row['name']} is set by foreach" for row in inv["foreach_vars"] if row["name"].lower() in call_variables]
    violations += [f"{row['line']}: the call variable ${row['name']} is passed by [ref]" for row in inv["ref_vars"] if row["name"].lower() in call_variables]
    # r15 iter 1: every write to what reaches a child process is pinned exactly (LANE_DIRTY_SAVE_PS_LAUNCH_WRITES)
    for assignment in inv["assignments"]:
        if assignment["drive"] or any(name.lower() in launch_variables for name in assignment["names"]):
            write = f"{assignment['left_text']} = {assignment['right_text']}"
            if assignment["operator"] != "Equals" or write not in LANE_DIRTY_SAVE_PS_LAUNCH_WRITES:
                violations.append(f"{assignment['line']}: a launch variable or env var written outside the pinned set: {write}")
    for row in inv["foreach_vars"]:
        if row["name"].lower() in launch_variables and f"foreach ${row['name']} in {row['in_text']}" not in LANE_DIRTY_SAVE_PS_LAUNCH_WRITES:
            violations.append(f"{row['line']}: a launch variable set by foreach outside the pinned set: ${row['name']} in {row['in_text']}")
    for parameter in inv["params"]:
        if parameter["name"].lower() in launch_variables and f"param ${parameter['name']}: {'/'.join(parameter['types'])}" not in LANE_DIRTY_SAVE_PS_LAUNCH_WRITES:
            violations.append(f"{parameter['line']}: a launch variable parameter outside the pinned set: ${parameter['name']}")
    violations += [f"{row['line']}: the launch variable ${row['name']} is passed by [ref]" for row in inv["ref_vars"] if row["name"].lower() in launch_variables]
    violations += [f"{row['line']}: an env or drive variable is passed by [ref]: ${row['name']}" for row in inv["ref_vars"] if ":" in row["name"]]  # r16 A4 sibling
    launch_objects = lowered(LANE_DIRTY_SAVE_PS_LAUNCH_OBJECTS)
    for use in inv["uses"]:
        if use["name"].lower() in launch_objects and f"{use['name'].lower()} @ {use['site']}" not in LANE_DIRTY_SAVE_PS_LAUNCH_OBJECT_USES:
            violations.append(f"{use['line']}: a launch object used at a site outside the pinned set (an alias or a call through it): ${use['name']} @ {use['site']}")
    return violations + _ps_binding_violations(inv)


def _ps_binding_violations(inv: dict) -> list[str]:
    """r16 (sol r15b B1): the provider / session-state binding class, default-deny (see the block above LANE_DIRTY_SAVE_PS_PROVIDER_CALLS)."""
    violations = []
    what = "provider/session-state binding access"
    for command in inv["commands"]:
        name = command["name"]
        if name is None:
            continue  # a command named at run time is already a violation
        base = name.rsplit("\\", 1)[-1].lower()
        if _PS_PROVIDER_FAMILY_RE.search(base) or base in _PS_PROVIDER_ALIASES:
            call = " ".join([name] + [element["text"] for element in command["elements"]])
            if call not in LANE_DIRTY_SAVE_PS_PROVIDER_CALLS or command["pipeline"] != 1:
                violations.append(f"{command['line']}: {what}: a provider cmdlet call outside the pinned set (its path, literal, dynamic or piped, can name "
                                  f"a Variable:, Function: or Alias: binding): {call}")
    for row in inv["strings"]:
        match = _PS_BINDING_DRIVE_RE.search(row["value"])
        if match:
            violations.append(f"{row['line']}: {what}: a string names the {match.group(1)}: drive: {row['value'][:80]!r}")
    violations += [f"{row['line']}: {what}: scoped variable syntax ${{{row['path']}}}" for row in inv["scoped_vars"]
                   if row["qualifier"].lower() in _PS_BINDING_QUALIFIERS]
    for use in inv["uses"]:
        key = use["name"].lower()
        if key in _PS_SESSION_VARIABLES:
            violations.append(f"{use['line']}: {what}: ${use['name']} reaches SessionState or a cmdlet's parameter defaults: {use['site']}")
        elif key in LANE_DIRTY_SAVE_PS_PROVIDER_RESULTS and f"{key} @ {use['site']}" not in LANE_DIRTY_SAVE_PS_PROVIDER_RESULT_USES:
            violations.append(f"{use['line']}: {what}: a provider call's result used at a site outside the pinned set: ${use['name']} @ {use['site']}")
    violations += [f"{member['line']}: {what}: .{member['name']}" for member in inv["members"]
                   if member["name"] is not None and member["name"].lower() in _PS_SESSION_MEMBERS]
    return violations


def _resolve_cs_escape(match: re.Match) -> str:
    code = int(match.group(0)[2:], 16)
    return chr(code) if code <= 0x10FFFF else chr(0xFFFD)  # reported as a violation already; an out-of-range escape must not raise (sol r14 hardening)


def _lane_dirty_save_text_violations(text: str) -> tuple[list[str], list[str]]:
    """(the text rules' violations, the Add-Type payload bodies) for one saver text: the rules the PowerShell AST cannot see."""
    violations = []
    for match in _NON_ASCII_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a non-ASCII character: U+{ord(match.group(0)):04X}")
    for match in _CONTROL_CHARACTER_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a control character: U+{ord(match.group(0)):04X}")
    for match in _CS_ESCAPE_RE.finditer(text):
        outside = " (outside Unicode)" if int(match.group(0)[2:], 16) > 0x10FFFF else ""
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: an escaped identifier character{outside}: {match.group(0)}")
    for match in _REFLECTION_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: a reflection lookup: {match.group(0)}")
    for match in _CS_DYNAMIC_CODE_RE.finditer(text):
        violations.append(f"{text.count(chr(10), 0, match.start()) + 1}: code compiled or loaded at run time: {match.group(0)}")
    lines = text.split("\n")
    add_types = [text.count("\n", 0, m.start()) + 1 for m in _ADD_TYPE_ANY_RE.finditer(text)]
    opens = [text.count("\n", 0, m.start()) + 1 for m in _HERE_STRING_OPEN_RE.finditer(text)]
    closes = [text.count("\n", 0, m.start()) + 1 for m in _HERE_STRING_CLOSE_RE.finditer(text)]
    if len(add_types) != 1 or lines[add_types[0] - 1].strip() != LANE_DIRTY_SAVE_ADD_TYPE_LINE:
        violations.append(f"Add-Type must appear exactly once, as the line {LANE_DIRTY_SAVE_ADD_TYPE_LINE!r}; found lines {add_types}")
    if opens != add_types[:1]:
        violations.append(f"the one here-string must be the Add-Type payload; here-strings open on lines {opens}")
    if len(closes) != 1 or lines[closes[0] - 1].rstrip() != "'@":
        violations.append(f"the payload's terminator must stand alone on its line as '@; terminators on lines {closes}")
    bodies = []
    for payload in _ADD_TYPE_PAYLOAD_RE.finditer(text):
        body, at = payload.group("body"), payload.start("body")
        bodies.append(body)
        for regex, what in ((_VERBATIM_IDENTIFIER_RE, "a verbatim identifier"), (_LITERAL_JOIN_RE, "a string built from pieces"),
                            (_PAYLOAD_PROCESS_RE, "a process start or run-time binding"), (_PAYLOAD_DIRECTIVE_RE, "a preprocessor directive")):
            for match in regex.finditer(body):
                violations.append(f"{text.count(chr(10), 0, at + match.start()) + 1}: {what} inside an Add-Type payload: {match.group(0)}")
        attributes = [match.group(0) for match in _PAYLOAD_DLLIMPORT_ATTRIBUTE_RE.finditer(body)]
        externs = len(re.findall(r"\bextern\b", body))
        if (len(_PAYLOAD_DLLIMPORT_RE.findall(body)) != len(attributes) or len(attributes) != externs
                or any(attribute not in LANE_DIRTY_SAVE_PAYLOAD_DLLIMPORTS for attribute in attributes)):
            violations.append(f"a process start or run-time binding inside an Add-Type payload: a DllImport outside the pinned libraries, or not one per extern "
                              f"({len(attributes)} attribute(s), {externs} extern(s)): {sorted(set(attributes) - LANE_DIRTY_SAVE_PAYLOAD_DLLIMPORTS)}")
        usings = [match.group(0).strip() for match in _PAYLOAD_USING_RE.finditer(body)]
        if usings != LANE_DIRTY_SAVE_PAYLOAD_USINGS:
            violations.append(f"the Add-Type payload's using directives are not exactly {LANE_DIRTY_SAVE_PAYLOAD_USINGS}: {usings}")
        for match in _PAYLOAD_QUALIFIED_RE.finditer(body):
            if re.sub(rf"{_CS_LINE_COMMENT}|\s+", "", match.group(0)) not in LANE_DIRTY_SAVE_PAYLOAD_QUALIFIED:
                violations.append(f"{text.count(chr(10), 0, at + match.start()) + 1}: a process start or run-time binding inside an Add-Type payload "
                                  f"(a qualified name outside the pinned set): {match.group(0)}")
    # the census proper runs on the text C# compiles: every escape resolved
    text = _CS_ESCAPE_RE.sub(_resolve_cs_escape, text)
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
    return violations, bodies


def lane_dirty_save_violations_many(sources: list[str | bytes]) -> list[list[str]]:
    """lane_dirty_save_violations for many saver texts with one PowerShell parser run. Bytes are decoded as UTF-8; a decode failure is a violation."""
    texts, results, bodies = [], [], []
    for source in sources:
        if isinstance(source, bytes):
            try:
                source = source.decode("utf-8")
            except UnicodeDecodeError as error:
                texts.append("")
                line = source.count(b"\n", 0, error.start) + 1
                results.append([f"{line}: not UTF-8 at byte {error.start}: {error.reason}"])
                bodies.append(None)
                continue
        texts.append(source)
        violations, payloads = _lane_dirty_save_text_violations(source)
        results.append(violations)
        bodies.append(payloads)
    inventories = _ps_inventories(texts)
    for index, payloads in enumerate(bodies):
        if payloads is None:
            continue
        if isinstance(inventories, str):
            results[index].append(f"the PowerShell AST census could not run, so nothing is proven: {inventories}")
        else:
            results[index] += _ps_ast_violations(inventories[index], payloads)
    return results


def lane_dirty_save_violations(source: str | bytes) -> list[str]:
    return lane_dirty_save_violations_many([source])[0]


# The saver's two launch loops, by their text: sol's repros name them as lines 569 and 667 of 48959b31, and a row must follow the line, not its number.
LAUNCH_569 = "    foreach ($a in (@('-C', $Wt) + $GitArgs)) { [void]$psi.ArgumentList.Add($a) }"
LAUNCH_667 = "    foreach ($a in (@('-C', $Wt) + $GitDiffArgs)) { [void]$psi.ArgumentList.Add($a) }"


def _line_of(lines: list[str], at: int | str) -> int:
    """`at` itself when it is a line number, else the 1-based number of the one line that is exactly `at` (a row whose anchor moved must not apply elsewhere)."""
    if isinstance(at, int):
        return at
    found = [number for number, line in enumerate(lines, 1) if line.rstrip("\r\n") == at]
    if len(found) != 1:
        raise AssertionError(f"the anchor is on {len(found)} lines, not one: {at!r}")
    return found[0]


def r15_census_rows(text: str) -> dict[str, tuple[str, str]]:
    """name -> (the saver with one sol r14 / fable r14 attack applied, a diagnostic it must produce or '' for any). Each returned [] from the r14 census."""
    lines = text.splitlines(keepends=True)
    at_line_50 = lambda inserted: "".join(lines[:49]) + inserted + "\n" + "".join(lines[49:])  # noqa: E731
    invoke_11 = "; $null = $m.Invoke($null,[object[]]@([IntPtr]::Zero,$null,[IntPtr]::Zero,[uint32]0,11))"
    swap = " { param([string]$TypeDefinition) Microsoft.PowerShell.Utility\\Add`-Type -TypeDefinition ($TypeDefinition -replace ', 10\\);', ', 11);') }"
    computed = "a member named at run time"
    call = "NtSetInformationFile(h, out io, info, (uint)size, 10);"
    launch_file = "$psi.FileName = @(Get-Command git -CommandType Application)[0].Source"
    launch_info = "$psi = New-Object System.Diagnostics.ProcessStartInfo"
    before_line = lambda at, inserted: "".join(lines[:_line_of(lines, at) - 1]) + inserted + "\n" + "".join(lines[_line_of(lines, at) - 1:])  # noqa: E731
    alias_attack = ("$launchAlias = $psi; $launchAlias.FileName = 'pwsh.exe'; $launchAlias.ArgumentList.Add('-NoProfile'); $launchAlias.ArgumentList.Add("
                    "'-NonInteractive'); $launchAlias.ArgumentList.Add('-Command'); $launchAlias.ArgumentList.Add(('New-Item -ItemType {0}{1} -Path "
                    "C:/fixture/out/alias -Value C:/fixture/src.bin; #' -f 'Hard','Link'))")
    return {
        # sol r14 B1: whitespace or a continuation after the member dot, then a computed name
        "B1 a space after the dot": (at_line_50("$m = [DirtyFinalPath]. ('Get'+'Method')(('NtSet'+'InformationFile'),40)" + invoke_11), computed),
        "B1 a tab after the dot": (at_line_50("$m = [DirtyFinalPath].\t('Get'+'Method')(('NtSet'+'InformationFile'),40)" + invoke_11), computed),
        "B1 a backtick-newline after the dot": (at_line_50("$m = [DirtyFinalPath].`\n('Get'+'Method')(('NtSet'+'InformationFile'),40)" + invoke_11), ""),
        "B1 a continued member named by a variable": (at_line_50("$n = 'Get' + 'Method'; $t = [DirtyFinalPath]; $m = $t. $n('x', 40)" + invoke_11), computed),
        "B1 a static member named by an expression after a space": (at_line_50("[DirtyFinalPath]:: ('Re'+'solve')($p)"), computed),
        # sol r14 B1 siblings: a variable command, an abbreviated -MemberName, escaped evaluation
        "B1 a variable command": (at_line_50("$c = 'Invoke-Ex'+'pression'; & $c $s"), "a command named at run time"),
        "B1 a variable command by dot-sourcing": (at_line_50("$c = 'x.ps1'; . $c"), "a command named at run time"),
        "B1 ForEach-Object -m": (at_line_50("$m = [DirtyFinalPath] | ForEach-Object -m $n 'x' 40"), "ForEach-Object with anything but scriptblocks"),
        "B1 ForEach-Object -Mem": (at_line_50("$m = [DirtyFinalPath] | ForEach-Object -Mem GetMethod 'x' 40"), "ForEach-Object with anything but scriptblocks"),
        "B1 ForEach-Object with a positional member name": (at_line_50("$m = [DirtyFinalPath] | ForEach-Object GetMethod 'x' 40"), "ForEach-Object with anything but scriptblocks"),
        "B1 Where-Object with a property name": (at_line_50("$m = @($a) | Where-Object Length -gt 0"), "Where-Object with anything but scriptblocks"),
        "B1 an escaped Invoke-Expression": (at_line_50("Invo`ke-Expression $s"), "a command outside the pinned set: Invoke-Expression"),
        "B1 an escaped iex": (at_line_50("ie`x $s"), "a command outside the pinned set: iex"),
        "B1 an escaped Invoke-Expression tail": (at_line_50("Invoke-Ex`pression $s"), "a command outside the pinned set: Invoke-Expression"),
        "B1 the % alias": (at_line_50("$m = [DirtyFinalPath] | % -m GetMethod 'x' 40"), "a command outside the pinned set: %"),
        "B1 a scriptblock created from text": (at_line_50("[scriptblock]::Create($s).InvokeReturnAsIs()"), "a static member outside the pinned set"),
        "B1 a splatted ForEach-Object": (at_line_50("$p = @{ MemberName = 'GetMethod' }; $m = [DirtyFinalPath] | ForEach-Object @p"), "splatted"),
        "B1 a magic ForEach by name": (at_line_50("$m = @([DirtyFinalPath]).ForEach('GetMethod', 'x')"), "a member outside the pinned set: .ForEach"),
        # sol r14 B2: an escaped function name rebinding Add-Type
        "B2 function Add`-Type": (text + "\nfunction Add`-Type" + swap + "\n", "a function outside the pinned set"),
        "B2 function A`dd-Type": (text + "\nfunction A`dd-Type" + swap + "\n", "a function outside the pinned set"),
        "B2 function Add-T`ype": (text + "\nfunction Add-T`ype" + swap + "\n", "a function outside the pinned set"),
        "B2 an escaped Set-Alias": (text + "\nfunction Invoke-CensusSwap" + swap + "\nSet`-Alias -Name Add-Type -Value Invoke-CensusSwap\n", "a command outside the pinned set: Set-Alias"),
        "B2 a function named like the saver's own, defined twice": (text + "\nfunction Get-DirtyKernelPath([string]$Path) { $Path }\n", "defined a second time"),
        # fable r14 finding 1 rows
        "F using module": ("using module C:\\x\\Shadow.psm1\n" + text, "a using statement"),
        "F #Requires -Modules": ("#Requires -Modules Shadow\n" + text, "#Requires loads"),
        "F Set-Item on a computed function: path": (text + "\nSet-Item -Path ('func'+'tion:Add-T'+'ype') -Value { param($TypeDefinition) }\n", "a process launch or env write outside the pinned set"),
        "F Set-Item on a literal function: path": (text + "\nSet-Item -LiteralPath function:Add-Type -Value { param($TypeDefinition) }\n", "a process launch or env write outside the pinned set"),
        "F a function: variable": (text + "\n${function:Add-Type} = { param($TypeDefinition) }\n", "only env: is allowed"),
        "F an AppDomain load": (at_line_50("[AppDomain]::CurrentDomain.Load($bytes)"), "a type outside the pinned set"),
        "F & a joined name": (at_line_50("$c = 'Invoke-Ex'+'pression'; & $c $s"), "a command named at run time"),
        # the call variables can hold only scriptblock literals
        "C a call variable given a string": (at_line_50("$guard = 'Invoke-Expression'"), "a call variable is given something other than a scriptblock"),
        "C a call variable in a multiple assignment": (at_line_50("$isUnc, $x = 'iex', 1"), "a call variable is given something other than a scriptblock"),
        "C a call variable set by -OutVariable": (at_line_50("Get-Item $p -OutVariable repoCheck"), "writes a variable by name"),
        "C a call variable set by -ov": (at_line_50("Get-Item $p -ov placeFailed"), "writes a variable by name"),
        "C a call variable set by foreach": (at_line_50("foreach ($guard in @('iex')) { }"), "set by foreach"),
        "C a call variable by [ref]": (at_line_50("$null = [DirtyFinalPath]::Pin([ref]$guard)"), "passed by [ref]"),
        "C a call variable parameter without [scriptblock]": (at_line_50("function Invoke-DirtyDestCheckpoint2($Guard) { & $Guard }"), "not typed [scriptblock]"),
        # anything else outside the pinned names
        "P New-Object of a computed type": (at_line_50("$a = New-Object ('System.Ref'+'lection.Assembly')"), "New-Object of a type or with arguments outside"),
        "P New-Object -TypeName": (at_line_50("$a = New-Object -TypeName System.Management.Automation.PowerShell"), "New-Object of a type or with arguments outside"),
        "P Get-Command Add-Type": (at_line_50("$c = Get-Command Add-Type"), "Get-Command other than"),
        "P a PowerShell class": (text + "\nclass Shadow { [void] Run() { } }\n", "defines code outside the census"),
        "P a quoted member name": (at_line_50("$m = [DirtyFinalPath].'GetMethod'('x', 40)"), "a member outside the pinned set: .GetMethod"),
        "P a second Add-Type by the call operator": (at_line_50("& 'Add-Type' -TypeDefinition $s"), "Add-Type must be invoked exactly once"),
        "P a module-qualified Add-Type": (at_line_50("Microsoft.PowerShell.Utility\\Add-Type -TypeDefinition $s"), "a command outside the pinned set"),
        # r15 iter 1 (adversary 99ae3557): a child process runs code the census never reads (the adversary's paths are replaced by fixture names: the census
        # never reads a path value, and no footage path belongs in code)
        "A1 Process::Start of pwsh": (text + "\n$null = [System.Diagnostics.Process]::Start('pwsh.exe', ('-NoProfile -NonInteractive -Command \"New-Item -ItemType "
                                      "{0}{1} -Path C:/fixture/out/alias -Value C:/fixture/src.bin\"' -f 'Hard','Link'))\n", "Process::Start used outside the pinned set"),
        "A1 a ProcessStartInfo FileName other than git": (text.replace(launch_file, "$psi.FileName = 'pwsh.exe'", 1), "written outside the pinned set"),
        "A1 a ProcessStartInfo constructor argument": (text.replace(launch_info, launch_info + " 'pwsh.exe'", 1), "New-Object of a type or with arguments outside"),
        "A1 ProcessStartInfo.Arguments": (text.replace(launch_file, launch_file + "; $psi.Arguments = '-c x'", 1), "written outside the pinned set"),
        "A1 a process started from the C# payload": (text.replace(call, call + '\nSystem.Diagnostics.Process.Start("pwsh.exe", "-c x");', 1), "a process start"),
        "A1 Activator from the C# payload": (text.replace(call, call + '\nobject o = Activator.CreateInstance(Type.GetType(n));', 1), "a process start"),
        "A2 git -c alias": (text + "\ngit -c ('alias.attack=!pwsh -NoProfile -NonInteractive -Command ''New-Item -ItemType {0}{1} -Path C:/fixture/out/alias -Value "
                            "C:/fixture/src.bin''' -f 'Hard','Link') attack\n", "a process launch or env write outside the pinned set"),
        "A2 an extra git argument on a pinned call": (text.replace("rev-parse --show-toplevel", "rev-parse --show-toplevel -c alias.x=!sh", 1),
                                                      "a process launch or env write outside the pinned set"),
        "A2 a git-capture call with other arguments": (at_line_50("$x = Invoke-DirtyGitCapture -Wt $Wt -GitArgs @('-c', 'alias.x=!sh', 'x')"),
                                                       "a process launch or env write outside the pinned set"),
        "A2 GIT_EXTERNAL_DIFF set": (at_line_50("$env:GIT_EXTERNAL_DIFF = 'pwsh -c x'"), "env var written outside the pinned set"),
        "A2 Set-Item of another env var": (at_line_50("Set-Item -LiteralPath Env:GIT_SSH_COMMAND -Value x"), "a process launch or env write outside the pinned set"),
        "A2 a git argument list widened": (at_line_50("$shape += '--ext-diff'"), "written outside the pinned set"),
        "A2 a foreach source widened": (text.replace("foreach ($side in 'HEAD', '--cached')", "foreach ($side in 'HEAD', '--ext-diff')", 1), "set by foreach outside"),
        "A2 a saved env value planted": (at_line_50("$savedEnv['GIT_EXTERNAL_DIFF'] = 'pwsh -c x'"), "written outside the pinned set"),
        "A2 a saved env value added by a method": (at_line_50("$savedEnv.Add('GIT_EXTERNAL_DIFF', 'pwsh -c x')"), "used outside the pinned set"),
        # r15 iter 2 (adversary 39a58424): an alias or a member-chain call changes the launch object; PowerShell's scripting API runs script from C#
        "B1 iter2 an alias of the launch object before line 569": (before_line(LAUNCH_569, alias_attack), "a launch object used at a site outside the pinned set"),
        "B1 iter2 an alias of the launch object before line 667": (before_line(LAUNCH_667, alias_attack), "a launch object used at a site outside the pinned set"),
        "B1 iter2 a direct ArgumentList.Add": (before_line(LAUNCH_569, "$psi.ArgumentList.Add('-c')"), "a launch object used at a site outside the pinned set"),
        "B1 iter2 a saved env alias": (at_line_50("$e = $savedEnv; $e['GIT_EXTERNAL_DIFF'] = 'pwsh -c x'"), "a launch object used at a site outside the pinned set"),
        "B1 iter2 a git dir rewritten through an alias": (at_line_50("$d = $gitDir; $d[0] = 'C:/fixture/other'"), "a launch object used at a site outside the pinned set"),
        "B1 iter2 a git argument array passed to a function": (at_line_50("Add-DirtyReuseHold $shape '--ext-diff'"), "a launch object used at a site outside the pinned set"),
        "B2 iter2 PowerShell.AddScript in the C# payload": (text.replace(call, call + '\nSystem.Management.Automation.PowerShell.Create().AddScript(string.Format('
                                                                    '"New-Item -ItemType {0}{1} -Path C:/fixture/out/alias -Value C:/fixture/src.bin", "Hard", "Link")).Invoke();', 1),
                                                         "a process start or run-time binding"),
        "B2 iter2 ScriptBlock.Create in the C# payload": (text.replace(call, call + "\nSystem.Management.Automation.ScriptBlock.Create(s).Invoke();", 1),
                                                         "a process start or run-time binding"),
    }


def r16_census_rows(text: str) -> dict[str, tuple[str, str]]:
    """name -> (the saver with one sol r15b B1 route applied, the binding-access diagnostic it must produce). The provider / session-state binding class:
    the Variable:, Function: and Alias: drives through any cmdlet or alias, scoped variable syntax, and SessionState reached from $ExecutionContext or $PSCmdlet."""
    lines = text.splitlines(keepends=True)
    before_line = lambda at, inserted: "".join(lines[:_line_of(lines, at) - 1]) + inserted + "\n" + "".join(lines[_line_of(lines, at) - 1:])  # noqa: E731
    at_line_50 = lambda inserted: before_line(50, inserted)  # noqa: E731
    unc_check = "        if ((& $isUnc $destReal) -and (& $isUnc $wtReal))"
    before_unc = lambda inserted: text.replace(unc_check, "        " + inserted + "\n" + unc_check, 1)  # noqa: E731
    digest_item = "        if ($item.LinkType -or"
    gitfile_first = "            $m = if ($first.Count -eq 1)"
    file_name = "(Get-Item Variable:psi).Value.FileName = 'C:/fixture/alternate.exe'"
    expected = "binding access"
    launch_info = "$psi = New-Object System.Diagnostics.ProcessStartInfo"
    call = "NtSetInformationFile(h, out io, info, (uint)size, 10);"
    return {
        # sol r15b B1, verbatim (the saver's launch sites are lines 569 and 667 at 48959b31; the UNC check is found by its text)
        "sol B1 Get-Item Variable:psi FileName before 569": (before_line(LAUNCH_569, file_name), expected),
        "sol B1 Get-Item Variable:psi ArgumentList.Add before 569": (before_line(LAUNCH_569, "(Get-Item Variable:psi).Value.ArgumentList.Add('--version')"), expected),
        "sol B1 Get-Item Variable:psi ArgumentList.Add before 667": (before_line(LAUNCH_667, "(Get-Item Variable:psi).Value.ArgumentList.Add('--version')"), expected),
        "sol B1 Get-Item Variable:isUnc before the UNC check": (before_unc("(Get-Item Variable:isUnc).Value = 'Get-Item'"), expected),
        # the cmdlet families, every way of naming the path
        "F -LiteralPath": (before_line(LAUNCH_569, "(Get-Item -LiteralPath Variable:psi).Value.FileName = 'x'"), expected),
        "F -Path quoted in mixed case": (before_line(LAUNCH_569, "(Get-Item -Path 'vArIaBlE:psi').Value.FileName = 'x'"), expected),
        "F -Path: colon form": (before_line(LAUNCH_569, "(Get-Item -Path:Variable:psi).Value.FileName = 'x'"), expected),
        "F positional double-quoted upper case": (before_line(LAUNCH_569, "(Get-Item \"VARIABLE:psi\").Value.FileName = 'x'"), expected),
        "F escaped in a double-quoted string": (before_line(LAUNCH_569, "(Get-Item \"Var`iable:psi\").Value.FileName = 'x'"), expected),
        "F provider-qualified": (before_line(LAUNCH_569, "(Get-Item -LiteralPath 'Microsoft.PowerShell.Core\\Variable::psi').Value.FileName = 'x'"), expected),
        "F module-qualified Get-Item": (before_line(LAUNCH_569, "(Microsoft.PowerShell.Management\\Get-Item Variable:psi).Value.FileName = 'x'"), expected),
        "F Get-Content": (before_line(LAUNCH_569, "(Get-Content Variable:psi).FileName = 'x'"), expected),
        "F Set-Item": (before_unc("Set-Item Variable:isUnc -Value 'Get-Item'"), expected),
        "F Set-Item on the alias drive": (at_line_50("Set-Item -LiteralPath Alias:Test-Path -Value Get-Item"), expected),
        "F New-Item on the function drive": (at_line_50("New-Item -Path Function:Test-Path -Value { $true }"), expected),
        "F Clear-Item": (before_unc("Clear-Item Variable:isUnc"), expected),
        "F Copy-Item": (before_unc("Copy-Item Variable:placeFailed Variable:isUnc"), expected),
        "F Rename-Item": (at_line_50("Rename-Item Function:Get-DirtyKernelPath Test-Path"), expected),
        "F Remove-Item": (before_unc("Remove-Item Variable:isUnc"), expected),
        "F Get-ChildItem": (before_line(LAUNCH_569, "(Get-ChildItem Variable: | Where-Object { $_.Name -eq 'psi' }).Value.FileName = 'x'"), expected),
        "F Get-ItemProperty": (before_line(LAUNCH_569, "$x = Get-ItemProperty -LiteralPath Variable:psi -Name Value"), expected),
        "F Set-ItemProperty": (before_unc("Set-ItemProperty Variable:isUnc -Name Value -Value 'Get-Item'"), expected),
        "F Set-Content on the function drive": (at_line_50("Set-Content -Path Function:Test-Path -Value 'param($LiteralPath) $true'"), expected),
        "F Get-Variable": (before_line(LAUNCH_569, "(Get-Variable psi -ValueOnly).FileName = 'x'"), expected),
        "F Set-Variable": (before_unc("Set-Variable -Name isUnc -Value 'Get-Item'"), expected),
        "F New-Variable -Force": (before_unc("New-Variable isUnc 'Get-Item' -Force"), expected),
        "F Remove-Variable": (before_unc("Remove-Variable isUnc"), expected),
        "F the gi alias": (before_line(LAUNCH_569, "(gi Variable:psi).Value.FileName = 'x'"), expected),
        "F the gv alias": (before_line(LAUNCH_569, "(gv psi).Value.FileName = 'x'"), expected),
        "F the sv alias": (before_unc("sv isUnc 'Get-Item'"), expected),
        # a dynamic path: the drive is not in the text
        "D a path built from pieces in a variable": (before_line(LAUNCH_569, "$p = 'Vari' + 'able:psi'; (Get-Item -LiteralPath $p).Value.FileName = 'x'"), expected),
        "D a drive in an expandable string": (before_line(LAUNCH_569, "$d = 'Variable'; (Get-Item \"${d}:psi\").Value.FileName = 'x'"), expected),
        "D a parenthesized path": (before_line(LAUNCH_569, "(Get-Item -LiteralPath ('Vari' + 'able:psi')).Value.FileName = 'x'"), expected),
        "D a path from the pipeline": (before_line(LAUNCH_569, "('Variable:psi' | Get-Item).Value.FileName = 'x'"), expected),
        "D a path added to a pinned call": (text.replace("Get-Item -LiteralPath $Path -Force", "Get-Item -LiteralPath $Path, 'Variable:psi' -Force", 1), expected),
        "D the pinned digest item written through": (text.replace(digest_item, "        $item.Value.FileName = 'x'\n" + digest_item, 1), expected),
        "D the pinned gitfile line written through": (text.replace(gitfile_first, "            $first[0].FileName = 'x'\n" + gitfile_first, 1), expected),
        # scoped variable syntax
        "V $variable:psi": (before_line(LAUNCH_569, "$variable:psi.FileName = 'x'"), expected),
        "V ${variable:psi}": (before_line(LAUNCH_569, "${variable:psi}.ArgumentList.Add('-c')"), expected),
        "V $VARIABLE:isUnc": (before_unc("$VARIABLE:isUnc = 'Get-Item'"), expected),
        "V ${Variable:isUnc}": (before_unc("${Variable:isUnc} = 'Get-Item'"), expected),
        "V ${Function:...}": (at_line_50("${Function:Get-DirtyKernelPath} = { 'x' }"), expected),
        "V $alias:": (at_line_50("$alias:gi = 'Invoke-Expression'"), expected),
        # SessionState
        "S $ExecutionContext PSVariable.Get": (before_line(LAUNCH_569, "$ExecutionContext.SessionState.PSVariable.Get('psi').Value.FileName = 'x'"), expected),
        "S $ExecutionContext PSVariable.Set": (before_unc("$ExecutionContext.SessionState.PSVariable.Set('isUnc', 'Get-Item')"), expected),
        "S lower-case $executioncontext GetValue": (before_line(LAUNCH_569, "$executioncontext.SessionState.PSVariable.GetValue('psi').ArgumentList.Add('-c')"), expected),
        "S $PSCmdlet": (before_unc("$PSCmdlet.SessionState.PSVariable.Set('isUnc', 'Get-Item')"), expected),
        "S SessionState through an alias": (before_unc("$s = $ExecutionContext.SessionState; $s.PSVariable.Set('isUnc', 'Get-Item')"), expected),
        "S ${global:ExecutionContext}": (before_unc("${global:ExecutionContext}.SessionState.PSVariable.Set('isUnc', 'Get-Item')"), expected),
        "S InvokeProvider": (before_line(LAUNCH_569, "$ExecutionContext.InvokeProvider.Item.Get('Variable:psi')[0].Value.FileName = 'x'"), expected),
        "S the default runspace": (before_unc("[runspace]::DefaultRunspace.SessionStateProxy.SetVariable('isUnc', 'Get-Item')"), expected),
        "S $Host.Runspace": (before_unc("$Host.Runspace.SessionStateProxy.SetVariable('isUnc', 'Get-Item')"), expected),
        # fable r15 hardening (LANE-DIRTY-SAVE-CENSUS-PREFERENCE-WRITES-1): a parameter default reaches a pinned cmdlet without touching its call text
        "S $PSDefaultParameterValues": (at_line_50("$PSDefaultParameterValues['Add-Type:OutputAssembly'] = 'x.dll'"), expected),
        "S $global:PSDefaultParameterValues": (at_line_50("$global:PSDefaultParameterValues.Add('Resolve-Path:Relative', $true)"), expected),
        # r16 iter 1 (adversary a11c4459): an assignment used as a value exports the object; a unary write; a VB / COM process start in the payload
        "iter1 A1 an assignment used as a value exports the launch object": (
            text.replace(launch_info, "$x = (" + launch_info + ")", 1).replace(LAUNCH_569, "    $x.FileName = 'alternate.exe'; [void]$x.ArgumentList.Add('--review-probe')\n" + LAUNCH_569, 1),
            "a launch object used at a site outside the pinned set"),
        "iter1 A1 an assignment passed as an argument": (text.replace(launch_info, "Add-DirtyReuseHold $list ($psi = New-Object System.Diagnostics.ProcessStartInfo)", 1),
                                                          "a launch object used at a site outside the pinned set"),
        "iter1 A1 a provider result exported by an outer assignment": (
            text.replace("        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop",
                         "        $Path = 'Vari' + 'able:psi'; $q = ($item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop); $q.Value.FileName = 'alternate.exe'", 1), expected),
        "iter1 A2 VisualBasic Interaction.Shell": (text.replace(call, call + '\nMicrosoft.VisualBasic.Interaction.Shell(string.Format("pwsh.exe -NoProfile -Command \\"New-Item '
                                                                 '-ItemType {0}{1} -Path C:/fixture/alias -Value C:/fixture/source.bin\\"", "Hard", "Link"), '
                                                                 'Microsoft.VisualBasic.AppWinStyle.Hide, true, -1);', 1), "a process start or run-time binding"),
        "iter1 A2 a using alias": (text.replace("using System.Text;", "using System.Text;\nusing VB = Microsoft.VisualBasic.Interaction;", 1),
                                   "using directives are not exactly"),
        "iter1 A2 a qualified name outside the set": (text.replace(call, call + '\nSystem.Net.WebClient c = null;', 1), "a qualified name outside the pinned set"),
        "iter1 A2 COM by ProgID": (text.replace(call, call + '\nobject o = Type.GetTypeFromProgID("WScript.Shell");', 1), "a process start or run-time binding"),
        "iter1 A4 ++$env:": (text + "\n++$env:GIT_CONFIG_COUNT\n", "written outside the pinned set"),
        "iter1 A4 $env:++": (text + "\n$env:GIT_CONFIG_COUNT++\n", "written outside the pinned set"),
        "iter1 A4 --$env:": (text + "\n--$env:GIT_CONFIG_COUNT\n", "written outside the pinned set"),
        "iter1 A4 $env:--": (text + "\n$env:GIT_CONFIG_COUNT--\n", "written outside the pinned set"),
        "iter1 A4 a launch variable incremented": (at_line_50("$gitDir[0]++"), "written outside the pinned set"),
        "iter1 A4 an env var by [ref]": (at_line_50("$null = [DirtyFinalPath]::Pin([ref]$env:GIT_CONFIG_COUNT)"), "passed by [ref]"),
        # r16 iter 2 (adversary 31a40a36): a switch, foreach or pipeline hands the value on; a comment inside a qualified name; a DllImport of another library
        "iter2 A1 a switch exports the launch object": (
            text.replace(launch_info, "switch (" + launch_info.strip() + ") { default { $x = $_ } }", 1).replace(
                LAUNCH_569, "    $x.FileName = 'C:/Windows/System32/cmd.exe'; [void]$x.ArgumentList.Add('/c')\n" + LAUNCH_569, 1),
            "a launch object used at a site outside the pinned set"),
        "iter2 A1 a foreach exports the launch object": (text.replace(launch_info, "foreach ($x in @((" + launch_info.strip() + "))) { $x.FileName = 'x' }", 1),
                                                         "a launch object used at a site outside the pinned set"),
        "iter2 A1 a pipeline exports the launch object": (before_line(LAUNCH_569, "$psi | ForEach-Object { $_.FileName = 'x' }"),
                                                          "a launch object used at a site outside the pinned set"),
        "iter2 A1 a pinned bare use piped on": (text.replace("$i -lt $gitDir.Count", "$i -lt ($gitDir | ForEach-Object { $_ }).Count", 1),
                                                "a launch object used at a site outside the pinned set"),
        "iter2 A2 a block comment inside a qualified name": (text.replace(call, call + '\nSystem/*gap*/.IO.File.WriteAllText("C:/fixture/wt/src/a.txt", "x");', 1),
                                                             "a process start or run-time binding"),
        "iter2 A2 a line comment inside a qualified name": (text.replace(call, call + '\nSystem// gap\n.IO.File.WriteAllText("C:/fixture/wt/src/a.txt", "x");', 1),
                                                            "a qualified name outside the pinned set"),
        "iter2 A3 a DllImport of another library": (text.replace('[DllImport("ntdll.dll")]', '[DllImport("C:/fixture/alternate.dll")]', 1), "a DllImport outside"),
        "iter2 A3 a DllImport by EntryPoint-free alias attribute": (text.replace('[DllImport("ntdll.dll")]', '[DllImport("ntdll.dll", ExactSpelling = true)]', 1),
                                                                     "a DllImport outside"),
        "iter2 A3 LoadLibrary": (text.replace(call, call + '\nLoadLibrary("C:/fixture/alternate.dll");', 1), "a process start or run-time binding"),
    }


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
        self.assertEqual(lane_dirty_save_violations(LANE_DIRTY_SAVE.read_bytes()), [])

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
        found = dict(zip(mutants, lane_dirty_save_violations_many(list(mutants.values()))))
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                self.assertTrue(found[name], f"the census missed: {name}")

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
        found = dict(zip(mutants, lane_dirty_save_violations_many([mutant for mutant, _ in mutants.values()])))
        for name, (mutant, code_point) in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                line = mutant.count("\n", 0, mutant.index(chr(code_point))) + 1
                self.assertIn(f"{line}: a non-ASCII character: U+{code_point:04X}", found[name], f"the census missed: {name}")
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
        found = dict(zip(mutants, lane_dirty_save_violations_many(list(mutants.values()))))
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                self.assertTrue(found[name], f"the census missed: {name}")

    def _assert_r15_rows_rejected(self) -> None:
        """Every row of r15_census_rows() gives a violation, and each B1 member row names its member as computed (the class rule, not a pinned-name miss)."""
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        rows = r15_census_rows(text)
        found = dict(zip(rows, lane_dirty_save_violations_many([mutant for mutant, _ in rows.values()])))
        for name, (mutant, expected) in rows.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(mutant, text, "the mutant did not apply")
                self.assertTrue(found[name], f"the census missed: {name}")
                if expected:
                    self.assertTrue(any(expected in violation for violation in found[name]), f"{name}: no {expected!r} in {found[name]}")

    def test_the_lane_dirty_saver_census_rejects_sol_r14_dispatch_and_rebinding(self) -> None:
        # r15 (sol r14 B1/B2 = r14 OPEN A1/A2, fable r14 finding 1): each row returned [] from the r14 regex census; the AST census rejects each by class.
        self._assert_r15_rows_rejected()

    def test_dropping_the_computed_member_rule_turns_the_b1_rows_red(self) -> None:
        # mutation test: without the computed-member rule the B1 rows lose their class diagnostic, so the row test above fails
        with mock.patch(f"{__name__}._ps_member_name_violation", lambda member: None):
            with self.assertRaises(AssertionError):
                self._assert_r15_rows_rejected_quietly()

    def _assert_r16_rows_rejected(self) -> None:
        """Every row of r16_census_rows() gives the binding-access diagnostic (the class rule, not a pinned-name miss)."""
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        rows = r16_census_rows(text)
        found = dict(zip(rows, lane_dirty_save_violations_many([mutant for mutant, _ in rows.values()])))
        missed = [name for name, (mutant, expected) in rows.items() if mutant == text or not any(expected in violation for violation in found[name])]
        self.assertEqual(missed, [], {name: found[name] for name in missed})

    def test_the_lane_dirty_saver_census_rejects_provider_and_session_state_binding_access(self) -> None:
        # r16 (sol r15b B1): a Variable:, Function: or Alias: provider path, scoped variable syntax or SessionState reaches a binding by another name
        self._assert_r16_rows_rejected()

    def test_dropping_the_binding_access_rule_turns_the_r16_rows_red(self) -> None:
        # mutation test: without the binding-access rule the r16 rows lose their class diagnostic, so the row test above fails
        with mock.patch(f"{__name__}._ps_binding_violations", lambda inv: []):
            with self.assertRaises(AssertionError):
                self._assert_r16_rows_rejected()

    def test_the_qualified_name_pattern_finds_what_the_r16_pattern_found_on_every_fixture(self) -> None:
        # r17 (CodeQL check-run 123193275334): the rewrite keeps every r16 match on the saver's payload, every census row's payload and every string literal
        # in this file (each inserted fixture fragment). The r16 pattern runs in a child, on these fixtures only (it finishes on them), never on the pumps.
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        sources = [text] + [mutant for rows in (r15_census_rows(text), r16_census_rows(text)) for mutant, _ in rows.values()]
        fixtures = [payload.group("body") for source in sources for payload in _ADD_TYPE_PAYLOAD_RE.finditer(source)]
        fixtures += [node.value for node in ast.walk(ast.parse(THIS_FILE.read_text(encoding="utf-8")))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        new = [[[match.start(), match.end(), match.group(0)] for match in _PAYLOAD_QUALIFIED_RE.finditer(fixture)] for fixture in fixtures]
        old = _finditer_in_child(_PAYLOAD_QUALIFIED_R16_PATTERN, fixtures, timeout=120)["found"]
        self.assertEqual(new, old)
        found = {match[2] for matches in new for match in matches}
        self.assertIn("System// gap\n.IO.File.WriteAllText", found, "the iter-2 line-comment fixture is among the compared ones")
        self.assertIn("System.Net.WebClient", found)

    def test_the_qualified_name_pattern_is_linear_on_the_codeql_pump_strings(self) -> None:
        # r17 (CodeQL check-run 123193275334): the r16 pattern ran past 20 s on N=24 of either pump (run red.txt); the rewrite finishes N=5000 at once.
        # Run in a child too, so a regression fails at the timeout instead of hanging the suite.
        pumps = ["System" + "//" * 5000 + "x", "System" + ". //" * 5000, "System" + "// x\n" * 5000 + "x", "System" + "//\r" * 5000 + "x",
                 "System" + "// x\r\n" * 5000 + "x"]
        result = _finditer_in_child(_PAYLOAD_QUALIFIED_RE.pattern, pumps, timeout=60)
        self.assertLess(result["seconds"], 1.0, result["seconds"])
        self.assertEqual(result["found"], [[]] * len(pumps))
        # control: a comment that runs to its line end (LF, CR, CRLF or LS) still sits inside a name, and the census strips it to the name C# compiles
        name = _PAYLOAD_QUALIFIED_RE.search("x = System// a .b\n// c\r . IO // d\r\n.File// e\u2028.Create(p);").group(0)
        self.assertEqual(re.sub(rf"{_CS_LINE_COMMENT}|\s+", "", name), "System.IO.File.Create")

    def test_the_lane_dirty_saver_census_rejects_what_the_r17_adversary_compiled_past_it(self) -> None:
        # r17 iter 1 (adversary 4b76fdba): each insertion returned [] while Roslyn parsed System.IO.File.WriteAllText. Built from the saver's raw bytes, so a
        # CR stays a CR (read_text would turn it into LF).
        text = LANE_DIRTY_SAVE.read_bytes().decode("utf-8")
        anchor = "public static string Identity(string path) {"
        self.assertIn(anchor, text)
        write = '.File.WriteAllText("C:/fixture/wt/src/a.txt", "x");'
        rows = {
            "A1 a CR ends the comment": ("\nSystem// gap\r.IO" + write, "a qualified name outside the pinned set"),
            "A1 a CR-only line between the parts": ("\nSystem\r.IO" + write, "a qualified name outside the pinned set"),
            "A1 a CR after the dot": ("\nSystem.// gap\rIO" + write, "a qualified name outside the pinned set"),
            "A1 a CRLF ends the comment": ("\nSystem// gap\r\n.IO" + write, "a qualified name outside the pinned set"),
            "A1 a NEL ends the comment": ("\nSystem// gap\x85.IO" + write, "a qualified name outside the pinned set"),
            "A1 an LS ends the comment": ("\nSystem// gap\u2028.IO" + write, "a qualified name outside the pinned set"),
            "A1 a PS ends the comment": ("\nSystem// gap\u2029.IO" + write, "a qualified name outside the pinned set"),
            "A2 directives before the dot": ("\nSystem\n#if true\n#endif\n.IO" + write, "a preprocessor directive"),
            "A2 directives after the dot": ("\nSystem.\n#if true\n#endif\nIO" + write, "a preprocessor directive"),
            "A2 a directive after a CR": ("\nSystem\r#if true\r#endif\r.IO" + write, "a preprocessor directive"),
            "A2 a #region between the parts": ("\nSystem\n  #region x\n  #endregion\n.IO" + write, "a preprocessor directive"),
        }
        mutants = {name: text.replace(anchor, anchor + inserted, 1) for name, (inserted, _) in rows.items()}
        found = dict(zip(rows, lane_dirty_save_violations_many(list(mutants.values()))))
        missed = [name for name, (_, expected) in rows.items() if mutants[name] == text or not any(expected in violation for violation in found[name])]
        self.assertEqual(missed, [], {name: found[name] for name in missed})
        # control: the saver as it is has no '#' in its payload and stays clean
        self.assertEqual(lane_dirty_save_violations(LANE_DIRTY_SAVE.read_bytes()), [])

    def _assert_r15_rows_rejected_quietly(self) -> None:
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        rows = {name: row for name, row in r15_census_rows(text).items() if name.startswith("B1 ") and row[1] == "a member named at run time"}
        self.assertGreaterEqual(len(rows), 3)
        for (name, (_, expected)), violations in zip(rows.items(), lane_dirty_save_violations_many([mutant for mutant, _ in rows.values()])):
            self.assertTrue(any(expected in violation for violation in violations), f"{name}: no {expected!r} in {violations}")

    def test_the_lane_dirty_saver_census_reports_bad_text_instead_of_raising(self) -> None:
        # sol r14 hardening / fable r14 finding 2: an out-of-range escape, a stray control character and invalid UTF-8 are diagnostics, never an exception
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        escape, control, undecodable = lane_dirty_save_violations_many([
            text + "\n# \\U00110000\n",
            text.replace("Add-Type -TypeDefinition @'", "\x0c\nAdd-Type -TypeDefinition @'"),
            LANE_DIRTY_SAVE.read_bytes() + b"\n# \xff\n",
        ])
        self.assertTrue(any("outside Unicode" in violation for violation in escape), escape)
        self.assertTrue(any("a control character: U+000C" in violation for violation in control), control)
        self.assertTrue(any("not UTF-8" in violation for violation in undecodable), undecodable)

    def test_the_lane_dirty_saver_census_fails_closed_without_the_parser(self) -> None:
        with mock.patch(f"{__name__}.shutil.which", lambda name: None):
            violations = lane_dirty_save_violations(LANE_DIRTY_SAVE.read_bytes())
        self.assertTrue(any("could not run" in violation for violation in violations), violations)

    def test_a_harmless_ascii_edit_to_the_saver_stays_clean(self) -> None:
        # control: the AST census pins names, not text, so a comment, a message string and whitespace change nothing
        text = LANE_DIRTY_SAVE.read_text(encoding="utf-8")
        edited = text.replace("reason=dest-unc-unproven", "reason=dest-unc-not-proven", 1) + "\n# a harmless trailing comment\n\n"
        self.assertNotEqual(edited, text)
        self.assertEqual(lane_dirty_save_violations(edited), [])
        self.assertEqual(sum(byte > 127 for byte in PS_AST_INVENTORY.read_bytes()), 0, "the inventory helper must stay ASCII")

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
