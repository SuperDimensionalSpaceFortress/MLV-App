"""OWNER-FOOTAGE-NO-HARDLINK-2: the DELETE class guard -- ownership proof is MANDATORY at every delete site.

The class PR #211 failed on: Remove-AttrCudaTree had an optional `-OwnedJournal` and, without one, read a
file's current identity and treated it as authority, so a legacy owner-clip hard link that had become the
last name of an owner recording was deleted by a build job's cleanup. The fix is structural, and this file
is the static guard that keeps it that way. It fails on:

  1. a delete-capable function whose ownership argument is optional, defaulted, or missing;
  2. any caller of a delete-capable helper that omits the proof;
  3. a delete-capable function nobody has classified (the census is pinned both ways);
  4. any pathname delete (Remove-Item, .Delete(), File::Delete) in the attribution tool family that is not on
     a commented allowlist with the reason its target cannot be a name of owner footage;
  5. any RECURSIVE delete (Remove-Item -Recurse, Directory.Delete(x, $true), rmtree, rd /s, rm -r) anywhere
     under tools/profiling that is not on a commented allowlist with that reason.

The behaviour (a legacy last name is left, a fresh tree is swept by proof, a submitted input is removed only
against its content hash) is proven on real NTFS in test_owner_footage_no_delete_without_proof.py.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILING = ROOT / "tools" / "profiling"
FAMILY = (PROFILING / "bachelor", PROFILING / "ultramagnus")

# ---- the helpers and the ownership argument each one REQUIRES ---------------------------------------

# helper -> regex that must appear in every call: the proof, passed by name.
HELPER_PROOF_ARGUMENT = {
    "Remove-AttrCudaFileByProof": r"-(?:FileId|NotBeforeFileTime|ExpectedSha256)\b",
    "Remove-AttrCudaFileById": r"-FileId\b",
    "Remove-AttrCudaPartialFile": r"-OwnedJournal\b",
    "Remove-AttrCudaTree": r"-OwnedJournal\b",
    "Remove-AttrCudaInputFileByContent": r"-ExpectedSha256\b",
    "New-AttrCudaOwnedRoot": r"-OwnedJournal\b",
}

# Parameters that carry ownership. Wherever one appears on a function that can delete, it must be Mandatory
# (in every parameter set it is declared in) and must carry no default value.
OWNERSHIP_PARAMETERS = ("FileId", "NotBeforeFileTime", "ExpectedSha256", "OwnedJournal", "Journal")

DELETE_PRIMITIVE = re.compile(
    r"\bRemove-Item\b|\[(?:System\.)?IO\.(?:File|Directory)\]::Delete\(|\bSetFileInformationByHandle\(\$handle, 4\b"
    r"|\bRemove-AttrCuda(?:FileByProof|FileById|PartialFile|Tree|InputFileByContent)\b|\bNew-AttrCudaOwnedRoot\b")

# Every function in the family's modules that contains a delete primitive or calls a delete helper, pinned.
# value = why it is safe: either it TAKES the proof as a mandatory parameter, or it deletes only an object it
# holds the creating-handle identity of in the same call, or it is a reasoned allowlist entry.
DELETE_CAPABLE_FUNCTIONS = {
    ("Attr3FootageStageJob.psm1", "New-Attr3FootageStageJob"):
        "the emitted job's Remove-StageOwnName calls Remove-AttrCudaFileById with the identity the SUBMITTER recorded "
        "(carried in the spec) and re-read on the agent's own handle; no identity is ever read off the name",
    ("AttrCudaArtifacts.psm1", "Assert-AttrCudaClosureComplete"):
        "removes only its own GUID-named temp file (attrcuda-census-<guid>.tmp in the machine temp directory)",
    ("AttrCudaArtifacts.psm1", "Resolve-AttrCudaSmokeRunnerClosure"):
        "removes only its own GUID-named temp file (attrcuda-closure-<guid>.tmp in the machine temp directory)",
    ("AttrCudaArtifacts.psm1", "Assert-AttrCudaWritableFileSlot"):
        "the publish-slot replace: refuses a directory, a reparse point and any name with a second name, then removes a "
        "plain single-name occupant of a FIXED derived publish name (a canonical artifact name, result.json, a job "
        "log) so the write that follows creates a fresh file; none of those names is a neutral owner-footage name",
    ("AttrCudaArtifacts.psm1", "New-AttrCudaOwnedFileStream"):
        "removes only the file it just created, by the identity read off that creating handle, when it cannot record it",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaPartialFile"): "takes -OwnedJournal (mandatory): the journal's proof or nothing",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaInputFileByContent"): "takes -ExpectedSha256 (mandatory): content proof",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaTree"): "takes -OwnedJournal (mandatory): per-entry proof, no recursive delete",
    ("AttrCudaArtifacts.psm1", "New-AttrCudaOwnedRoot"): "takes -OwnedJournal (mandatory); an unproven standing tree is MOVED, not deleted",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaFileByProof"): "the one function that sets a delete disposition; every parameter set makes a proof mandatory",
    ("AttrCudaArtifacts.psm1", "Remove-AttrCudaFileById"): "takes -FileId (mandatory): creator-recorded identity",
    ("AttrCudaWorkRetention.psm1", "Invoke-AttrCudaWorkRetention"):
        "takes -OwnedJournal (mandatory); a superseded sibling .work-<sha12> is touched only when its head published a "
        "build.json and no file in it carries container magic or exceeds 2 GiB (a tracked fixture, matched by git blob id, "
        "is the one exemption), and then only through Remove-AttrCudaTree with that build dir's own journal "
        "(<sibling build dir>\\<leaf of -OwnedJournal>): no pathname or recursive delete, an unproven name is LEFT, and the "
        "directory stands unless every entry was proven",
    ("AttrCudaOwnerFootage.psm1", "Send-AttrCudaOwnerFootagePartToStaging"):
        "deletes only the partial it created, by the identity read off its own CreateNew handle",
    ("AttrCudaOwnerFootage.psm1", "Test-AttrCudaSymlinkCapability"):
        "deletes only the probe files it created, by the identities read off their creating handles / journalled",
    ("AttrCudaOwnerFootage.psm1", "New-AttrCudaOwnerFootageView"):
        "deletes only the view entry it just created, by the identity read off its creating handle",
    ("AttrCudaOwnerFootage.psm1", "Clear-AttrCudaOwnerFootageLeftovers"):
        "deletes only entries the journal names, by the journalled identity; a legacy neutral entry is LEFT_LEGACY",
    ("AttrCudaOwnerFootage.psm1", "Close-AttrCudaOwnerFootageWorkspace"):
        "deletes only view entries this job created, by their recorded entry identities",
}

# ---- 4. every pathname delete in the family -----------------------------------------------------------

PATHNAME_DELETE = re.compile(r"\bRemove-Item\b|\[(?:System\.)?IO\.(?:File|Directory)\]::Delete\(")

# (file name, source line with comments removed and whitespace trimmed) -> why its target cannot be a name of
# owner footage. Exactly these, nothing else.
PATHNAME_DELETE_ALLOWED = {
    ("attr3-footage-presence-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "generator side: the emitted <job id>.job.ps1 at the generator's own -OutDir, rewritten each emission.",
    ("attr3-footage-read-rate-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "generator side: the emitted <job id>.job.ps1 at the generator's own -OutDir, rewritten each emission.",
    ("attr3-footage-stage.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "generator side: the emitted <job id>.job.ps1 at the generator's own -OutDir, rewritten each emission.",
    ("attr3-footage-stage.ps1", "[IO.Directory]::Delete($shareStageDir, $false)"):
        "non-recursive: removes the attempt's staging directory only when it is already EMPTY (a directory holds no file name).",
    ("AttrCudaArtifacts.psm1", "if (Test-Path -LiteralPath $tempFile) { Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue }"):
        "a GUID-named temp file this very call created in the machine temp directory (two functions share the line).",
    ("AttrCudaArtifacts.psm1", "Remove-Item -LiteralPath $full -Force -Confirm:$false"):
        "Assert-AttrCudaWritableFileSlot: the publish-slot replace of a fixed derived publish name, after the directory / "
        "reparse-point / second-name refusals; see DELETE_CAPABLE_FUNCTIONS.",
    ("AttrCudaArtifacts.psm1", "try { [IO.Directory]::Delete($entry.FullName, $false) } catch { }"):
        "Remove-AttrCudaTree: non-recursive, EMPTY directories only.",
    ("AttrCudaArtifacts.psm1", "try { [IO.Directory]::Delete($root.FullName, $false) } catch { }"):
        "Remove-AttrCudaTree: non-recursive, the emptied root directory only.",
    ("playback-attr-3-cuda-assemble.ps1", "if (Test-Path -LiteralPath $stagedPkgPath) { Remove-Item -LiteralPath $stagedPkgPath -Force }"):
        "the packaging step's own staged zip inside $Scratch, under the work tree this run created FRESH and recorded "
        "(New-AttrCudaOwnedRoot); the name is derived from the commit and cannot pre-exist from an older build.",
    ("playback-attr-3-cuda-compile-job.ps1", "if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }"):
        "RETIRED generator (it throws before emitting): the source archive it is about to recreate under its own -OutDir.",
    ("playback-attr-3-cuda-compile-job.ps1", "if (Test-Path -LiteralPath $stagedPkgPath) { Remove-Item -LiteralPath $stagedPkgPath -Force }"):
        "RETIRED job body (unrun): its own staged zip under its own scratch directory.",
    ("playback-attr-3-cuda-compile-job.ps1", "Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue"):
        "RETIRED job body (unrun): its own .partial files in the cache.",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }"):
        "generator side: the emitted <job id>.job.ps1 at the generator's own -OutFile, rewritten each emission.",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $displayIdentityTemp) { Remove-Item -LiteralPath $displayIdentityTemp -Force -ErrorAction SilentlyContinue }"):
        "the display-identity helper's own temp copy under $Work (a path built from a fixed name inside the run's per-run work tree).",
    ("playback-attr-3-cuda-job.ps1", "if (Test-Path -LiteralPath $contactSheetComposerTempPath) { Remove-Item -LiteralPath $contactSheetComposerTempPath -Force }"):
        "the contact-sheet composer script's own temp copy under $Work (a fixed name inside the run's per-run work tree).",
    ("playback-attr-3-cuda-dll-job.ps1", "if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }"):
        "generator side: the source archive about to be recreated under the generator's own -OutDir.",
    ("um-display-mapping-probe-job.ps1", "Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue"):
        "recursive: see RECURSIVE_DELETE_ALLOWED (a GUID temp directory extracted from the probe zip).",
}

# ---- 5. every RECURSIVE delete under tools/profiling -----------------------------------------------------

RECURSIVE_DELETE_RE = re.compile(
    r"\b(?:Remove-Item|ri|rm|rmdir|del|erase|rd)\b[^\n|;]*?\s-(?:r|re|rec|recu|recur|recurs|recurse)\b"
    r"|\[(?:System\.)?IO\.Directory\]::Delete\([^)]*,\s*\$true\s*\)"
    r"|\bshutil\.rmtree\b|\brmtree\s*\("
    r"|\b(?:rd|rmdir)\s+/s\b|\brm\s+-\w*r\w*\b"
    r"|\bRemoveDirectory\w*\b.*\brecurs",
    re.IGNORECASE,
)
RECURSIVE_SCANNED_SUFFIXES = {".ps1", ".psm1", ".py", ".cmd", ".bat", ".sh"}

# (path relative to tools/profiling, source line with comments removed and trimmed) -> why the tree it removes
# cannot hold a name of owner footage.
RECURSIVE_DELETE_ALLOWED = {
    ("import-local-cuda-proof-result.ps1", "Remove-Item -LiteralPath $extractRoot -Recurse -Force"):
        "the extraction root of an imported evidence PACKET (<packet>-<utc stamp>), created from a zip by this script "
        "under its own destination root; never a view directory and never given a footage path.",
    ("install-openssh-standalone.ps1", "if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }"):
        "$env:TEMP\\osshx, the throwaway extraction directory of the OpenSSH installer zip.",
    ("invoke-ultramagnus-cdng-export-evidence.ps1", "if (Test-Path -LiteralPath `$packetRoot) { Remove-Item -LiteralPath `$packetRoot -Recurse -Force }"):
        "inside the emitted job text: '<runRoot>\\packet', the evidence packet directory the job rebuilds each run "
        "under its own per-run root; no owner path flows into it.",
    ("invoke-ultramagnus-cdng-export-evidence.ps1", "Remove-Item -LiteralPath $expandedRoot -Recurse -Force"):
        "'<output root>\\imported\\packet-<stamp>': the unpacked evidence packet this script extracts itself.",
    ("test-gui-smoke-color-artifact-scan.ps1", "Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue"):
        "a GUID-named temp directory holding the test's own synthetic PNGs.",
    ("ultramagnus/um-display-mapping-probe-job.ps1", "Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue"):
        "'qscreen-probe-<guid>' in $env:TEMP, extracted from the probe zip by this job a few lines above.",
}


def strip_comments(text: str) -> list[str]:
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


def join_continuations(lines: list[str]) -> list[tuple[int, str]]:
    """PowerShell backtick continuations joined onto their first line (number kept)."""
    joined: list[tuple[int, str]] = []
    pending = None
    for number, line in enumerate(lines, start=1):
        if pending is not None:
            first, text = pending
            text = text.rstrip("`").rstrip() + " " + line
            pending = (first, text) if line.endswith("`") else None
            if pending is None:
                joined.append((first, text))
            continue
        if line.endswith("`"):
            pending = (number, line)
        else:
            joined.append((number, line))
    if pending is not None:
        joined.append(pending)
    return joined


def module_functions(path: Path) -> dict[str, str]:
    """Function name -> body text, by the column-0 contract the job embedder itself relies on."""
    text = path.read_bytes().decode("utf-8", errors="ignore").replace("\r\n", "\n")
    return {m.group(1): m.group(2) for m in re.finditer(r"(?ms)^function\s+([\w-]+)\s*\{(.*?)^\}", text)}


def ownership_parameter_problems(name: str, body: str) -> list[str]:
    """Problems with the ownership parameters of ONE function body: optional, defaulted, or absent proof."""
    problems = []
    param_match = re.search(r"(?ms)^\s*param\s*\((.*?)^\s*\)\s*$", body)
    declared = param_match.group(1) if param_match else ""
    for parameter in OWNERSHIP_PARAMETERS:
        for line in declared.splitlines():
            if re.search(rf"\${parameter}\b", line) and "[Parameter(" in line:
                if "Mandatory = $true" not in line:
                    problems.append(f"{name}: -{parameter} is not Mandatory")
                if re.search(rf"\${parameter}\s*=", line):
                    problems.append(f"{name}: -{parameter} has a default value")
            elif re.search(rf"\${parameter}\b", line) and "[Parameter(" not in line:
                problems.append(f"{name}: -{parameter} is declared without a [Parameter(Mandatory = $true)] attribute")
    return problems


def helper_call_problems(label: str, lines: list[str]) -> list[str]:
    """Calls to a delete helper that omit the proof (embed-list names and definitions are not calls)."""
    problems = []
    for number, line in join_continuations(lines):
        if not line or line.lstrip().startswith(("function ", "Export-ModuleMember")):
            continue
        for helper, proof in HELPER_PROOF_ARGUMENT.items():
            for match in re.finditer(rf"(?<![\w'\"-]){re.escape(helper)}(?![\w-])", line):
                before = line[: match.start()].rstrip()
                if before.endswith(("'", '"')) or re.match(rf"^['\"]?{re.escape(helper)}['\"]?\s*,", line.lstrip()):
                    continue  # a name in an embed / export list
                call = line[match.end():]
                if not re.search(proof, call):
                    problems.append(f"{label}:{number}: {helper} called without its proof ({proof}): {line}")
    return problems


def family_sources(*suffixes: str) -> list[Path]:
    found = []
    for directory in FAMILY:
        for suffix in suffixes:
            found += sorted(directory.glob(f"*{suffix}"))
    return found


class OwnershipProofIsMandatoryTests(unittest.TestCase):
    def test_every_helper_declares_its_ownership_argument_mandatory_with_no_default(self) -> None:
        functions = module_functions(PROFILING / "bachelor" / "AttrCudaArtifacts.psm1")
        problems = []
        for helper in HELPER_PROOF_ARGUMENT:
            self.assertIn(helper, functions, f"{helper} is no longer defined")
            body = functions[helper]
            problems += ownership_parameter_problems(helper, body)
            self.assertTrue(
                any(re.search(rf"\${p}\b", body) for p in OWNERSHIP_PARAMETERS),
                f"{helper} declares no ownership parameter at all")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_handle_delete_has_no_parameter_set_without_a_proof(self) -> None:
        body = module_functions(PROFILING / "bachelor" / "AttrCudaArtifacts.psm1")["Remove-AttrCudaFileByProof"]
        declared = re.search(r"(?ms)^\s*param\s*\((.*?)^\s*\)\s*$", body).group(1)
        self.assertNotIn("DefaultParameterSetName", body, "a default parameter set lets a call omit the proof")
        for line in declared.splitlines():
            if "ParameterSetName" in line and "Mandatory = $true" not in line:
                self.assertNotRegex(line, r"\$(FileId|NotBeforeFileTime|ExpectedSha256)\b", line)
        sets = re.findall(r"ParameterSetName = '(\w+)'", declared)
        self.assertEqual(sorted(set(sets)), ["ById", "Content", "CreatedAfter"])

    def test_every_call_of_a_delete_helper_names_its_proof(self) -> None:
        problems = []
        for directory in (PROFILING,):
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() not in {".ps1", ".psm1"} or "test" in path.name.lower():
                    continue
                text = path.read_bytes().decode("utf-8", errors="ignore")
                text = re.sub(r"__[A-Z0-9_]+__", "PLACEHOLDER", text)
                problems += helper_call_problems(path.relative_to(ROOT).as_posix(), strip_comments(text))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_census_of_delete_capable_functions_is_pinned_both_ways(self) -> None:
        found = set()
        for path in family_sources(".psm1"):
            for name, body in module_functions(path).items():
                if DELETE_PRIMITIVE.search("\n".join(strip_comments(body))):
                    found.add((path.name, name))
        unclassified = sorted(found - set(DELETE_CAPABLE_FUNCTIONS))
        stale = sorted(set(DELETE_CAPABLE_FUNCTIONS) - found)
        self.assertEqual(unclassified, [], "a delete-capable function is not classified: add it to DELETE_CAPABLE_FUNCTIONS "
                         "with the proof it takes (a mandatory ownership parameter) or the reason it needs none")
        self.assertEqual(stale, [], "a classified function no longer deletes: remove it from DELETE_CAPABLE_FUNCTIONS")

    def test_no_classified_delete_capable_function_has_an_optional_ownership_argument(self) -> None:
        # The class, not the six helpers: EVERY function that can delete (the pinned census above) is held to the
        # same rule, so an ownership argument added later as `[string]$OwnedJournal = ''` is a failure here.
        problems = []
        for path in family_sources(".psm1"):
            for name, body in module_functions(path).items():
                if (path.name, name) in DELETE_CAPABLE_FUNCTIONS:
                    problems += ownership_parameter_problems(name, body)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_no_function_named_remove_or_delete_hides_from_the_census(self) -> None:
        for path in family_sources(".psm1"):
            for name in module_functions(path):
                if re.match(r"(?:Remove|Delete|Clear|Clean|Purge)-", name):
                    self.assertIn((path.name, name), DELETE_CAPABLE_FUNCTIONS, f"{name} is named like a delete but is not classified")

    def test_the_checker_catches_what_it_claims_to(self) -> None:
        optional = (
            "    [CmdletBinding()]\n    param(\n        [Parameter(Mandatory = $true)][string]$Path,\n"
            "        [string]$OwnedJournal = ''\n    )\n")
        self.assertTrue(ownership_parameter_problems("Remove-X", optional), "an optional, defaulted journal was not flagged")
        defaulted = "    param(\n        [Parameter(Mandatory = $true)][string]$OwnedJournal = 'x'\n    )\n"
        self.assertTrue(ownership_parameter_problems("Remove-X", defaulted), "a defaulted mandatory journal was not flagged")
        not_mandatory = "    param(\n        [Parameter()][string]$OwnedJournal\n    )\n"
        self.assertTrue(ownership_parameter_problems("Remove-X", not_mandatory))
        fine = "    param(\n        [Parameter(Mandatory = $true)][string]$OwnedJournal\n    )\n"
        self.assertEqual(ownership_parameter_problems("Remove-X", fine), [])
        for omitting in (
            "Remove-AttrCudaTree -TrustedRoot $a -Path $b",
            "[void](Remove-AttrCudaPartialFile -TrustedRoot $a -Path $b)",
            "Remove-AttrCudaFileById -Path $p",
            "Remove-AttrCudaFileByProof -Path $p",
            "$ok = Remove-AttrCudaInputFileByContent -TrustedRoot $a -Path $p",
            "New-AttrCudaOwnedRoot -TrustedRoot $a -Path $p",
        ):
            self.assertTrue(helper_call_problems("probe", [omitting]), f"an omitted proof was not flagged: {omitting}")
        # a backtick continuation must not hide the proof (nor invent one)
        self.assertEqual(helper_call_problems("probe", ["Remove-AttrCudaTree -TrustedRoot $a `", "    -Path $b -OwnedJournal $j"]), [])
        self.assertTrue(helper_call_problems("probe", ["Remove-AttrCudaTree -TrustedRoot $a `", "    -Path $b"]))
        for fine_line in (
            "Remove-AttrCudaTree -TrustedRoot $a -Path $b -OwnedJournal $j",
            "'Remove-AttrCudaTree',",
            "    Remove-AttrCudaTree, `",
            "function Remove-AttrCudaTree {",
            "Remove-AttrCudaFileByProof -Path $p -NotBeforeFileTime $t",
        ):
            self.assertEqual(helper_call_problems("probe", [fine_line]), [], f"false positive: {fine_line}")


class PathnameDeleteCensusTests(unittest.TestCase):
    def _hits(self) -> set[tuple[str, str]]:
        found = set()
        for path in family_sources(".ps1", ".psm1"):
            for line in strip_comments(path.read_bytes().decode("utf-8", errors="ignore")):
                if line and PATHNAME_DELETE.search(line):
                    found.add((path.name, line))
        return found

    def test_every_pathname_delete_in_the_attribution_family_is_allowlisted_with_a_reason(self) -> None:
        unexplained = sorted(self._hits() - set(PATHNAME_DELETE_ALLOWED))
        self.assertEqual(
            unexplained, [],
            "a pathname delete appeared in the attribution tool family. A pathname is not an identity: delete through "
            "Remove-AttrCudaFileByProof / a proof-taking helper, or add the exact line to PATHNAME_DELETE_ALLOWED with "
            "the reason its target cannot be a name of owner footage.")

    def test_every_allowlisted_pathname_delete_still_matches_a_line(self) -> None:
        stale = sorted(set(PATHNAME_DELETE_ALLOWED) - self._hits())
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from PATHNAME_DELETE_ALLOWED")


class RecursiveDeleteCensusTests(unittest.TestCase):
    def _hits(self) -> set[tuple[str, str]]:
        found = set()
        for path in sorted(PROFILING.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in RECURSIVE_SCANNED_SUFFIXES:
                continue
            if path.name.startswith("test_"):
                continue  # Python tests build and clean their own synthetic temp trees
            relative = path.relative_to(PROFILING).as_posix()
            if path.suffix.lower() == ".py":
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            else:
                lines = strip_comments(path.read_bytes().decode("utf-8", errors="ignore"))
            for line in lines:
                stripped = line.strip()
                if stripped and RECURSIVE_DELETE_RE.search(stripped):
                    found.add((relative, stripped))
        return found

    def test_no_recursive_delete_under_tools_profiling_except_the_allowlisted_reasoned_ones(self) -> None:
        unexplained = sorted(self._hits() - set(RECURSIVE_DELETE_ALLOWED))
        self.assertEqual(
            unexplained, [],
            "a recursive delete (Remove-Item -Recurse, Directory.Delete(x, $true), rmtree, rd /s, rm -r) appeared under "
            "tools/profiling. A recursive pathname delete removes whatever names the tree holds -- including a legacy "
            "hard link that is the last name of an owner recording. Use New-AttrCudaOwnedRoot + Remove-AttrCudaTree, or "
            "add the exact line to RECURSIVE_DELETE_ALLOWED with the reason the tree cannot hold a footage name.")

    def test_every_allowlisted_recursive_delete_still_matches_a_line(self) -> None:
        stale = sorted(set(RECURSIVE_DELETE_ALLOWED) - self._hits())
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from RECURSIVE_DELETE_ALLOWED")

    def test_the_scan_catches_every_spelling_it_claims_to(self) -> None:
        for line in (
            "Remove-Item -LiteralPath $p -Recurse -Force",
            "Remove-Item $p -r -Force",
            "Remove-Item -Force -Recurse -LiteralPath $p",
            "rm -Recurse $p",
            "[IO.Directory]::Delete($p, $true)",
            "[System.IO.Directory]::Delete($p, $true)",
            "shutil.rmtree(path)",
            "rd /s /q C:\\x",
            "rm -rf build",
        ):
            self.assertTrue(RECURSIVE_DELETE_RE.search(line), f"the scan missed: {line}")
        for line in (
            "Remove-Item -LiteralPath $emitPath -Force",
            "[IO.Directory]::Delete($shareStageDir, $false)",
            "Remove-AttrCudaTree -TrustedRoot $a -Path $b -OwnedJournal $j",
        ):
            self.assertFalse(RECURSIVE_DELETE_RE.search(line), f"false positive: {line}")

    def test_the_retired_compile_job_still_refuses_before_its_unrun_body(self) -> None:
        text = (PROFILING / "bachelor" / "playback-attr-3-cuda-compile-job.ps1").read_text(encoding="utf-8")
        self.assertIn("RETIRED: refuse before doing anything", text)
        self.assertLess(text.index("throw @\""), text.index("$template = @'"))
        self.assertNotRegex("\n".join(strip_comments(text)), RECURSIVE_DELETE_RE)


if __name__ == "__main__":
    unittest.main()
