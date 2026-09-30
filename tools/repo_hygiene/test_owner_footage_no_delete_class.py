"""UM-OWNER-FOOTAGE-CROSS-VOLUME-2: no job deletes, renames over or replaces a name of owner footage.

PR #200 removed its private hard links with "read NumberOfLinks, then delete the name". Windows has
no atomic "delete this name only if another name of the file remains", and the owner's source name
stays replaceable while our no-share-delete handle is held, so that delete could remove the LAST name
of the owner's clip (sol r2; tools/repo_hygiene/test_playback_attr_3_cuda_behaviour.py reproduces it
on real NTFS). Two rounds narrowed the check and the class survived, so the class is REMOVED: this
file is its static guard.

It scans the three sources that can reach a private link name -- the owner-footage module, the
attribution job (generator AND the job template it emits) and the bind-proof module (generator AND
its emitted template) -- for every primitive that destroys or re-points a name, and fails on any hit
that is not one of the named exceptions below. An exception is an exact source line with the reason
it cannot reach a link name; a changed or added line is a failure until it is reviewed and listed, and
an exception that no longer matches anything is a failure too (so the list cannot rot into cover).

Comments are ignored (the scan strips them); string literals are NOT (a primitive spelled inside a
string still counts), except the function-name lists the generators embed, which are listed below.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACHELOR = ROOT / "tools" / "profiling" / "bachelor"

OWNER_FOOTAGE_MODULE = BACHELOR / "AttrCudaOwnerFootage.psm1"
ATTRIBUTION_JOB = BACHELOR / "playback-attr-3-cuda-job.ps1"
BIND_PROOF_MODULE = BACHELOR / "Attr3FootageBindProofJob.psm1"
SCANNED = (OWNER_FOOTAGE_MODULE, ATTRIBUTION_JOB, BIND_PROOF_MODULE)

# Every spelling that deletes a name, renames one, replaces one, or asks the OS to delete on close.
PRIMITIVES = (
    r"\bRemove-Item\b",
    # The empty-only directory remover is judged by its own (non-recursive) Delete line, below.
    r"\bRemove-AttrCuda(?!EmptyOwnerFootageDirectory\b)\w*",
    r"\bMove-Item\b",
    r"\bRename-Item\b",
    r"(?:\.|::)Delete\s*\(",
    r"\bDeleteFileW?\b",
    r"\bRemoveDirectoryW?\b",
    r"\bSetFileInformationByHandle\b",
    r"\bFileDisposition\w*",
    r"\bMoveFileExW?\b",
    r"\[(?:System\.)?IO\.(?:File|Directory)\]::(?:Move|Replace)\b",
    r"\.MoveTo\s*\(",
    r"\bDeleteOnClose\b",
    r"0x0*4000000\b",   # FILE_FLAG_DELETE_ON_CLOSE
    r"0x0*10000\b",     # DELETE access right (what a by-handle delete is opened with)
    r"\b(?:rmdir|erase)\b",
    r"\bcmd(?:\.exe)?\s+/c\b",
)
PRIMITIVE_RE = re.compile("|".join(f"(?:{p})" for p in PRIMITIVES), re.IGNORECASE)

# (file name, the source line with comments removed and whitespace trimmed) -> why it cannot reach a
# private link name of owner footage. Exactly these, nothing else.
ALLOWED = {
    # ---- AttrCudaOwnerFootage.psm1 -------------------------------------------------------------
    ("AttrCudaOwnerFootage.psm1", "try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}"):
        "Send-AttrCudaOwnerFootagePartToStaging: the '<slot>.partial' BYTE COPY on an agent share, created by "
        "this call with FileMode.CreateNew (never a hard link, never the owner's name); four sites, same line.",
    ("AttrCudaOwnerFootage.psm1", "[IO.File]::Move($partialPath, $finalPath, $false)"):
        "Send-AttrCudaOwnerFootagePartToStaging: the non-overwriting publish ($false = no replace) of that same "
        "staged copy into its neutral slot on the share; neither name is a link to owner footage.",
    ("AttrCudaOwnerFootage.psm1", "try { [IO.Directory]::Delete($relocated, $false) } catch {}"):
        "Resolve-AttrCudaOwnerFootageDirectory: removes the EMPTY directory this call created a moment ago "
        "(non-recursive: the OS refuses a directory holding any entry, so no name can go); two sites, same line.",
    ("AttrCudaOwnerFootage.psm1", "[IO.Directory]::Delete($Directory, $false)"):
        "Remove-AttrCudaEmptyOwnerFootageDirectory: non-recursive, so the OS itself refuses a non-empty "
        "directory (any link name, sidecar, anything); the directory must also still be the pinned one.",
    # ---- playback-attr-3-cuda-job.ps1 (generator + emitted template) --------------------------
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "generator side: the job FILE it is about to rewrite at its own -OutFile; not a job-runtime delete.",
    ("playback-attr-3-cuda-job.ps1", "'Remove-AttrCudaTree',"):
        "a function NAME in the list of verifier functions embedded into the job (the code is exercised only "
        "through the guarded call below).",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $displayIdentityTemp) { Remove-Item -LiteralPath $displayIdentityTemp -Force -ErrorAction SilentlyContinue }"):
        "a temp file the job wrote itself under its own scratch for the display-identity probe.",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $contactSheetComposerTempPath) { Remove-Item -LiteralPath $contactSheetComposerTempPath -Force }"):
        "a temp file the job wrote itself under its own scratch for the contact-sheet composer.",
    ("playback-attr-3-cuda-job.ps1", "Remove-AttrCudaTree -TrustedRoot 'C:\\mlvtmp' -Path $Work"):
        "THE ONLY recursive delete in the job: the pre-clean of $Work, reached only after the guard above it "
        "refuses (exit 28, nothing removed) any $Work that already holds a private 'owner-clip' link directory; "
        "a fresh $Work cannot hold a link to owner footage because links are created later, in this run. "
        "test_the_only_recursive_delete_in_the_job_refuses_a_work_tree_holding_a_link_directory proves the guard.",
    # ---- Attr3FootageBindProofJob.psm1: nothing -- its emitted job deletes no file at all ------
}

# Functions the generators embed that may carry a Remove- name; anything else named Remove-* in an
# embedded list is a new delete and fails the test.
ALLOWED_EMBEDDED_REMOVE_NAMES = {"Remove-AttrCudaTree", "Remove-AttrCudaEmptyOwnerFootageDirectory"}


def _strip_comments(text: str) -> list[str]:
    """Source lines with block comments and '#' line comments removed (quotes respected), keeping
    line numbering. Here-string bodies are kept: the emitted job templates live in them."""
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


def _hits(path: Path) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(_strip_comments(path.read_text(encoding="utf-8")), start=1):
        if line and PRIMITIVE_RE.search(line):
            found.append((number, line))
    return found


class OwnerFootageNoDeleteClassTests(unittest.TestCase):
    def test_no_delete_or_rename_over_primitive_is_reachable_except_the_named_exceptions(self) -> None:
        unexplained = []
        for path in SCANNED:
            for number, line in _hits(path):
                if (path.name, line) not in ALLOWED:
                    unexplained.append(f"{path.name}:{number}: {line}")
        self.assertEqual(
            unexplained, [],
            "a delete / rename / replace primitive appeared in a source that can reach a private link name of "
            "owner footage. No job may delete such a name (see this module's docstring); if the line cannot "
            "reach a link name, add it to ALLOWED with the reason.")

    def test_every_named_exception_still_matches_a_line(self) -> None:
        present = {(path.name, line) for path in SCANNED for _, line in _hits(path)}
        stale = sorted(key for key in ALLOWED if key not in present)
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from ALLOWED")

    def test_the_bind_proof_job_deletes_nothing_at_all(self) -> None:
        self.assertEqual(_hits(BIND_PROOF_MODULE), [])

    def test_the_removed_link_delete_functions_are_gone_everywhere_in_tools(self) -> None:
        names = (
            "Remove-AttrCudaOwnerFootageLinkName",
            "Remove-AttrCudaOwnerFootageRecordedLinks",
            "Remove-AttrCudaOwnerFootageRelocatedDirectory",
        )
        this_file = Path(__file__).resolve()
        for path in (ROOT / "tools").rglob("*"):
            if path.suffix.lower() not in {".ps1", ".psm1", ".py"} or path.resolve() == this_file:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in names:
                if name in text:
                    # A comment or docstring that names a removed function is history, not a call.
                    live = [line for line in _strip_comments(text) if name in line]
                    if path.suffix.lower() == ".py":
                        live = [line for line in text.splitlines() if name in line and "assert" in line.lower() and "NotIn" not in line]
                    self.assertEqual(live, [], f"{path.relative_to(ROOT)} still references {name}")

    def test_the_embedded_function_lists_carry_no_other_remove_function(self) -> None:
        for path in (ATTRIBUTION_JOB, BIND_PROOF_MODULE):
            text = path.read_text(encoding="utf-8")
            named = set(re.findall(r"'(Remove-[A-Za-z0-9]+)'", text))
            self.assertLessEqual(named, ALLOWED_EMBEDDED_REMOVE_NAMES, path.name)

    def test_the_module_has_no_delete_access_handle_and_no_delete_p_invoke(self) -> None:
        text = "\n".join(_strip_comments(OWNER_FOOTAGE_MODULE.read_text(encoding="utf-8")))
        self.assertNotIn("SetFileInformationByHandle", text)
        self.assertIsNone(re.search(r"0x0*10000\b", text))


if __name__ == "__main__":
    unittest.main()
