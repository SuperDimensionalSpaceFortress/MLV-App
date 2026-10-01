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
    r"\bNtSetInformationFile\b",
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
    text = path.read_text(encoding="utf-8", errors="ignore")
    hits: list[tuple[int, str]] = []
    if path.suffix.lower() == ".py":
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
        self.assertIn("Remove-AttrCudaTree -TrustedRoot 'C:\\mlvtmp' -Path $Work -OwnedJournal $OwnerJournal", code)
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
