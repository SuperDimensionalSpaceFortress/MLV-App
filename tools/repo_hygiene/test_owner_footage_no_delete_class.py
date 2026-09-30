"""UM-OWNER-FOOTAGE-CROSS-VOLUME-2: no job deletes, renames over or replaces a name of owner footage.

PR #200 removed its private hard links with "read NumberOfLinks, then delete the name". Windows has
no atomic "delete this name only if another name of the file remains", and the owner's source name
stays replaceable while our no-share-delete handle is held, so that delete could remove the LAST name
of the owner's clip (sol r2; tools/repo_hygiene/test_playback_attr_3_cuda_behaviour.py reproduces it
on real NTFS). Two rounds narrowed the check and the class survived, so the class is REMOVED: this
file is its static guard. Round 2 (sol r1) removed the two deletes that were left -- the job-start
recursive pre-clean of $Work (a hard link under ANY other name, or a concurrent job sharing $Work,
defeated its name guard) and the staging job's delete of a published copy after a failed verify (the
pathname can be swapped for a hard link to owner footage between the verify and the delete) -- and
made $Work unique by construction and create-new. No job deletes a tree any more.

It scans every source that can reach a private link name or a name in the owner's directory -- the
owner-footage module, the attribution job (generator AND the job template it emits), the bind-proof
module (generator AND template), the footage-staging job module and its CLI, the presence and
read-rate generators, and the shared AttrCudaArtifacts.psm1 whose functions the jobs embed -- for
every primitive that destroys, truncates or re-points a name, and fails on any hit that is not one of
the named exceptions below. An exception is an exact source line with the reason it cannot reach a
name of owner footage; a changed or added line is a failure until it is reviewed and listed, and an
exception that no longer matches anything is a failure too (so the list cannot rot into cover).

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
STAGE_JOB_MODULE = BACHELOR / "Attr3FootageStageJob.psm1"
STAGE_CLI = BACHELOR / "attr3-footage-stage.ps1"
PRESENCE_CLI = BACHELOR / "attr3-footage-presence-job.ps1"
READ_RATE_CLI = BACHELOR / "attr3-footage-read-rate-job.ps1"
ARTIFACTS_MODULE = BACHELOR / "AttrCudaArtifacts.psm1"
SCANNED = (
    OWNER_FOOTAGE_MODULE, ATTRIBUTION_JOB, BIND_PROOF_MODULE, STAGE_JOB_MODULE, STAGE_CLI,
    PRESENCE_CLI, READ_RATE_CLI, ARTIFACTS_MODULE,
)

# Every spelling that deletes a name, truncates a file, renames one, replaces one, or asks the OS to
# delete on close -- including the PowerShell aliases (rm ri rd del erase rmdir) and Clear-Item.
PRIMITIVES = (
    # Any Remove-* command or function; the empty-only directory remover is judged by its own
    # (non-recursive) Delete line, below. (Remove-Item, Remove-ItemProperty, Remove-AttrCuda*, ...)
    r"\bRemove-(?!AttrCudaEmptyOwnerFootageDirectory\b)\w+",
    r"\bClear-(?:Item|Content)\b",
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
    # The built-in aliases of Remove-Item. Not preceded by a word character, '$', '.' or '-' (so
    # '$del', '.rm' and '-del' are not commands) and not followed by a word character or '-'.
    r"(?<![\w$.-])(?:rm|ri|rd|del|erase|rmdir)(?![\w-])",
    r"\bcmd(?:\.exe)?\s+/c\b",
)
PRIMITIVE_RE = re.compile("|".join(f"(?:{p})" for p in PRIMITIVES), re.IGNORECASE)

# (file name, the source line with comments removed and whitespace trimmed) -> why it cannot reach a
# name of owner footage. Exactly these, nothing else.
#
# The two shapes behind most entries, stated once:
#   * JOB-UNIQUE SCRATCH. $Work and $Pub carry a random component (see $JobNonce) and $Work is
#     created create-new, so no two jobs share them; the private link directory is never a target of
#     a Publish-* call or of any entry below.
#   * CREATED BY THIS CALL. A byte copy or partial this call made with FileMode.CreateNew in a
#     directory that is agent-private and unique to the job/attempt (the staging share slot), which no
#     job ever links to owner footage.
ALLOWED = {
    # ---- AttrCudaOwnerFootage.psm1 -------------------------------------------------------------
    ("AttrCudaOwnerFootage.psm1", "try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}"):
        "Send-AttrCudaOwnerFootagePartToStaging: the '<slot>.partial' BYTE COPY on the agent share, created by "
        "this call with FileMode.CreateNew in the per-job staging directory (agent-private, never linked to "
        "owner footage, never the owner's directory); four sites, same line.",
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
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $displayIdentityTemp) { Remove-Item -LiteralPath $displayIdentityTemp -Force -ErrorAction SilentlyContinue }"):
        "a temp file the job wrote itself under its own scratch ($Work\\.job-tmp) for the display-identity probe.",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $contactSheetComposerTempPath) { Remove-Item -LiteralPath $contactSheetComposerTempPath -Force }"):
        "a temp file the job wrote itself under its own scratch for the contact-sheet composer.",
    # ---- Attr3FootageStageJob.psm1 -------------------------------------------------------------
    ("Attr3FootageStageJob.psm1", "'Remove-AttrCudaPartialFile'"):
        "a function NAME in the list of verifiers embedded into the staging job (exercised only by the "
        "share-side cleanup call below).",
    ("Attr3FootageStageJob.psm1", "if ($CleanupPath) { [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $CleanupPath -WarningAction SilentlyContinue) }"):
        "the SHARE-side staged neutral copy ('part-<n>') under <AgentRoot>\\footage-stage\\<unique job id>\\: an "
        "agent-private directory created for this job, holding only byte copies the CLI made with CreateNew; "
        "Remove-AttrCudaPartialFile refuses a directory or reparse point and a path outside the trusted root. "
        "NEVER the owner's directory: the published copy at the spec path and this attempt's target-volume "
        "partial are NOT deleted any more (left in place and recorded -- see Write-StageLeftoverRecord).",
    # ---- attr3-footage-stage.ps1 (the CLI, on the submitting host) -----------------------------
    ("attr3-footage-stage.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "a temp JSON the CLI itself wrote in the local temp directory (resolver output); not footage.",
    ("attr3-footage-stage.ps1", "$partialRemoved = Remove-AttrCudaPartialFile -TrustedRoot $shareStageRoot -Path $createdPath -WarningAction SilentlyContinue"):
        "the CLI's cleanup of the share staging slots IT created for this attempt, under "
        "<share>\\footage-stage\\<unique job id>\\ (agent-private byte copies); never the owner's directory.",
    ("attr3-footage-stage.ps1", "function Remove-Attr3FootageStageAttemptResidue {"):
        "the CLI's wrapper around the two share-side removals listed here (the slots it created and the "
        "per-job staging directory); both act inside <share>\\footage-stage\\<unique job id>\\ only.",
    ("attr3-footage-stage.ps1", "Remove-Attr3FootageStageAttemptResidue"):
        "a call of that wrapper (two sites, same line).",
    ("attr3-footage-stage.ps1", "[IO.Directory]::Delete($shareStageDir, $false)"):
        "the per-job staging directory, non-recursive: the OS refuses it while any entry remains.",
    # ---- attr3-footage-presence-job.ps1 / attr3-footage-read-rate-job.ps1 (generators) ---------
    ("attr3-footage-presence-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "a temp JSON the generator wrote in the local temp directory (resolver output); not footage.",
    ("attr3-footage-read-rate-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "a temp JSON the generator wrote in the local temp directory (resolver output); not footage.",
    # ---- AttrCudaArtifacts.psm1 (shared verifiers; the jobs embed a subset) --------------------
    ("AttrCudaArtifacts.psm1", "if (Test-Path -LiteralPath $tempFile) { Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue }"):
        "a GUID-named temp file under [IO.Path]::GetTempPath() that the same function wrote a line above "
        "(committed git blob bytes for a census); two sites, same line; generator side, not footage.",
    ("AttrCudaArtifacts.psm1", "Remove-Item -LiteralPath $full -Force -Confirm:$false"):
        "Assert-AttrCudaWritableFileSlot: clears an existing PLAIN FILE in a PUBLISH slot so the write that follows "
        "creates a fresh file. Every caller names a destination inside $Pub or $Work (the publish-write scan "
        "attr3_publish_write_scan.ps1 proves it for the attribution template), both unique per job and never a "
        "target of a link to owner footage; a directory or reparse point is refused first.",
    ("AttrCudaArtifacts.psm1", "Move-Item -LiteralPath $Source -Destination $slot -Force"):
        "Publish-AttrCudaFileMove: renames a file into a slot-checked publish destination. NOT embedded by the "
        "attribution job, the bind proof or the staging job (test_the_owner_jobs_embed_no_tree_delete_and_no_file_move "
        "enforces it); only the build-route jobs embed it, and they never touch footage.",
    ("AttrCudaArtifacts.psm1", "[IO.File]::Move($Source, $slot, $false)"):
        "Publish-AttrCudaFileMoveNonOverwriting: the non-overwriting ($false = no replace) same-volume publish of "
        "the staging job's own per-attempt partial (a name this attempt created, carrying the job id) onto the "
        "spec path; it can never replace an existing name.",
    ("AttrCudaArtifacts.psm1", "[IO.Directory]::Move($Source, $slot)"):
        "Publish-AttrCudaDirectoryMoveNonOverwriting: a DIRECTORY rename onto a slot that must not exist "
        "(Directory.Move refuses an existing destination); used by the build-route jobs for their own trees.",
    ("AttrCudaArtifacts.psm1", "function Remove-AttrCudaPartialFile {"):
        "the definition of the guarded single-file remover (see its call site in the staging job above).",
    ("AttrCudaArtifacts.psm1", "Remove-Item -LiteralPath $Path -Force -Confirm:$false -ErrorAction SilentlyContinue"):
        "Remove-AttrCudaPartialFile: one plain file, only after the trusted-root ancestor chain check and only when "
        "the leaf is neither a directory nor a reparse point. Its callers are listed above and all name a share "
        "staging slot or a job-unique partial, never a name in the owner's directory.",
    ("AttrCudaArtifacts.psm1", "function Remove-AttrCudaTree {"):
        "the definition of the recursive remover. It is NOT embedded by any owner-footage job (the attribution "
        "job, the bind proof and the staging job); test_the_owner_jobs_embed_no_tree_delete_and_no_file_move "
        "enforces it. Its remaining callers are the build-route jobs' own work trees under the agent root, which "
        "never hold a private owner link.",
    ("AttrCudaArtifacts.psm1", "Remove-Item -LiteralPath $Path -Recurse -Force -Confirm:$false"):
        "Remove-AttrCudaTree's one recursive delete, after the ancestor chain check and a reparse-point walk; see "
        "the function entry above for why no owner job can reach it.",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaPartialFile, `"):
        "the module's export list (a function NAME).",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaTree, `"):
        "the module's export list (a function NAME).",
}


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


def _hits_in(text: str) -> list[tuple[int, str]]:
    return [(number, line) for number, line in enumerate(_strip_comments(text), start=1) if line and PRIMITIVE_RE.search(line)]


def _hits(path: Path) -> list[tuple[int, str]]:
    return _hits_in(path.read_text(encoding="utf-8"))


class OwnerFootageNoDeleteClassTests(unittest.TestCase):
    def test_no_delete_or_rename_over_primitive_is_reachable_except_the_named_exceptions(self) -> None:
        unexplained = []
        for path in SCANNED:
            for number, line in _hits(path):
                if (path.name, line) not in ALLOWED:
                    unexplained.append(f"{path.name}:{number}: {line}")
        self.assertEqual(
            unexplained, [],
            "a delete / rename / replace / truncate primitive appeared in a source that can reach a name of "
            "owner footage. No job may delete such a name (see this module's docstring); if the line cannot "
            "reach one, add it to ALLOWED with the reason.")

    def test_every_named_exception_still_matches_a_line(self) -> None:
        present = {(path.name, line) for path in SCANNED for _, line in _hits(path)}
        stale = sorted(key for key in ALLOWED if key not in present)
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from ALLOWED")

    def test_the_scanner_catches_every_alias_and_spelling_it_claims_to(self) -> None:
        # fable r1 hardening: the old primitive list missed rm/ri/rd/del/erase/rmdir and Clear-Item, and
        # the scan did not cover the staging module or the embedded shared functions.
        must_hit = [
            "rm -LiteralPath $linkPath -Force",
            "ri $linkPath",
            "rd -Recurse $dir",
            "del $linkPath",
            "erase $linkPath",
            "rmdir $dir",
            "Clear-Item -LiteralPath $linkPath",
            "Clear-Content -LiteralPath $linkPath",
            "Remove-ItemProperty -Path $p -Name x",
            "Remove-Item -LiteralPath $p",
            "Get-ChildItem $d | rm",
            "[IO.File]::Delete($linkPath)",
            "[System.IO.File]::Delete($linkPath)",
            "[IO.Directory]::Delete($dir, $true)",
            "$info.Delete()",
            "Move-Item -LiteralPath $a -Destination $b",
            "[IO.File]::Replace($a, $b, $null)",
            "cmd /c del $p",
        ]
        for line in must_hit:
            self.assertEqual(len(_hits_in(line)), 1, f"the scan missed: {line}")
        must_not_hit = [
            "$model = Get-Item -LiteralPath $p",
            "$delta = 1",
            "$form = 'x'",
            "Test-Path -LiteralPath $p",
            "New-Item -ItemType HardLink -Path $a -Value $b",
            "[void]$set.Remove($name)",
            "# rm -LiteralPath $linkPath",
        ]
        for line in must_not_hit:
            self.assertEqual(_hits_in(line), [], f"false positive: {line}")

    def test_the_scan_covers_every_source_the_owner_jobs_are_built_from(self) -> None:
        names = {path.name for path in SCANNED}
        for required in ("Attr3FootageStageJob.psm1", "AttrCudaArtifacts.psm1", "attr3-footage-stage.ps1",
                         "playback-attr-3-cuda-job.ps1", "Attr3FootageBindProofJob.psm1", "AttrCudaOwnerFootage.psm1"):
            self.assertIn(required, names)
            self.assertTrue((BACHELOR / required).is_file())

    def test_the_bind_proof_job_deletes_nothing_at_all(self) -> None:
        self.assertEqual(_hits(BIND_PROOF_MODULE), [])

    def test_the_staging_job_no_longer_deletes_by_pathname_in_the_owners_directory(self) -> None:
        # sol r1 blocker 2: its only file deletes left are the share-side staged copy (allowlisted above).
        text = "\n".join(_strip_comments(STAGE_JOB_MODULE.read_text(encoding="utf-8")))
        self.assertNotIn("Remove-Item", text)
        self.assertNotIn("Delete(", text)
        self.assertIn("Write-StageLeftoverRecord", text)

    def test_the_attribution_job_no_longer_carries_a_pre_clean(self) -> None:
        # sol r1 blocker 1: no recursive delete of $Work, no name guard for one, create-new instead.
        text = "\n".join(_strip_comments(ATTRIBUTION_JOB.read_text(encoding="utf-8")))
        self.assertNotIn("Remove-AttrCudaTree", text)
        self.assertNotIn("OWNER_LINK_DIRECTORY_PRESENT", text)
        self.assertIn("$JobNonce = [Guid]::NewGuid()", text)
        self.assertIn("New-Item -ItemType Directory -Path $Work -ErrorAction Stop", text)
        self.assertNotIn("New-Item -ItemType Directory -Path $Work -Force", text)

    def test_the_owner_jobs_embed_no_tree_delete_and_no_file_move(self) -> None:
        for path in (ATTRIBUTION_JOB, BIND_PROOF_MODULE, STAGE_JOB_MODULE):
            text = path.read_text(encoding="utf-8")
            embedded_names = set(re.findall(r"'([A-Za-z]+-[A-Za-z0-9]+)'", text))
            for forbidden in ("Remove-AttrCudaTree", "Publish-AttrCudaFileMove", "Publish-AttrCudaDirectoryMoveNonOverwriting"):
                self.assertNotIn(forbidden, embedded_names, f"{path.name} embeds {forbidden}")

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

    def test_the_module_has_no_delete_access_handle_and_no_delete_p_invoke(self) -> None:
        text = "\n".join(_strip_comments(OWNER_FOOTAGE_MODULE.read_text(encoding="utf-8")))
        self.assertNotIn("SetFileInformationByHandle", text)
        self.assertIsNone(re.search(r"0x0*10000\b", text))


if __name__ == "__main__":
    unittest.main()
