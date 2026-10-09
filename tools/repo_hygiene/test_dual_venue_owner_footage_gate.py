"""DVE-OWNER-FOOTAGE-GATE-1: the reviewed owner-footage switch reads true only while the guards that justify it exist.

THE SWITCH. tools/profiling/dual-venue/venues.json `ownerFootage.cleanupClassGone` is the second reviewed gate
(after the owner's per-venue consent record) on a dual-venue owner-clip leg. It named one defect class: a job
deleting, hard-linking or relocating a NAME of owner footage. That class is removed on master by
OWNER-FOOTAGE-NO-HARDLINK-1/-2 (PR #214, which superseded the closed UM-OWNER-FOOTAGE-CROSS-VOLUME-2, PR #203): no
code path creates a hard link to owner footage (a view is a symbolic link or a verified byte copy), the original is
pinned FileShare.Read for the run, and every delete helper demands a creator-recorded ownership proof.

WHAT THIS FILE BINDS. A boolean in a JSON file proves nothing by itself, so two things are pinned here:

  1. GATE <-> GUARDS. While the tracked table says `cleanupClassGone: true`, every named guard test is still
     DEFINED (and not skipped / expected-failure) in its file. Delete or rename one and the gate may not stay open.
     The value must be a real JSON boolean (the runner casts it with [bool], and [bool]'string' is true) and the
     refusal token for the closed case is kept. This is the closest honest binding: it cannot prove the guard tests
     PASS (CI does that, each is its own required test), only that the evidence the gate rests on has not been
     deleted out from under it.
  2. THE ROUTE'S OWN DELETE SURFACE. The delete census in test_owner_footage_delete_class.py covers
     tools/profiling/{bachelor,ultramagnus}. An owner leg also runs the dual-venue runner, um-run.ps1 / UmRunDrop.psm1
     (the only writer to a venue share), and the smoke launcher with its length gate. Every delete / rename-over /
     move primitive in THOSE sources is listed below with the reason its target cannot be a name of owner footage; a
     new one fails until it is reviewed and listed, and a listed line that no longer exists fails too.

Both checks are exercised against deliberately broken inputs (mutation self-tests below).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILING = ROOT / "tools" / "profiling"
HYGIENE = Path(__file__).resolve().parent
VENUES = PROFILING / "dual-venue" / "venues.json"

REFUSAL_TOKEN = "OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2"

# test file (in tools/repo_hygiene) -> the test methods that each pin one clause of "the cleanup class is gone".
GUARD_TESTS = {
    "test_owner_footage_no_hardlink_class.py": (
        # nothing under tools/ creates a hard link; a view is only a symlink or a copy and the original is pinned first
        "test_no_tracked_tool_creates_a_hard_link_except_the_named_exceptions",
        "test_the_attribution_job_never_builds_a_view_with_a_hard_link_and_always_pins_first",
        "test_the_view_is_only_ever_a_symlink_or_a_copy",
        # every owner-capable delete goes through the identity/ownership primitive, never by pathname or recursively
        "test_the_owner_jobs_delete_only_through_the_identity_checked_primitive",
        "test_no_owner_capable_source_deletes_by_pathname_or_recursively",
        "test_the_owner_job_sweeps_its_work_tree_only_through_the_journal",
        "test_the_leftover_sweep_deletes_only_journalled_entries",
    ),
    "test_owner_footage_delete_class.py": (
        "test_every_helper_declares_its_ownership_argument_mandatory_with_no_default",
        "test_the_handle_delete_has_no_parameter_set_without_a_proof",
        "test_every_call_of_a_delete_helper_names_its_proof",
        "test_every_pathname_delete_in_the_attribution_family_is_allowlisted_with_a_reason",
        "test_no_recursive_delete_under_tools_profiling_except_the_allowlisted_reasoned_ones",
    ),
    "test_owner_footage_no_delete_without_proof.py": (
        "test_the_handle_delete_has_no_mode_without_a_proof",
        "test_the_tree_remover_needs_a_journal",
        "test_sol_r2_a_legacy_last_name_under_a_standing_work_tree_is_moved_aside_never_deleted",
        "test_the_assembler_leaves_a_legacy_last_name_under_its_work_tree",
    ),
    "test_owner_footage_identity_delete.py": (
        "test_a_name_swapped_for_a_hard_link_to_the_owners_file_is_left_and_both_names_survive",
        "test_a_symlink_is_deleted_as_itself_and_its_target_survives",
        "test_a_copy_view_directory_survives_a_recursive_sweep_with_the_original_intact",
    ),
    "test_owner_footage_creator_ownership.py": (
        "test_a_legacy_entry_that_became_the_last_name_after_the_owner_rerecorded_is_never_deleted",
        "test_the_view_builder_journals_the_entry_from_its_creating_handle",
    ),
}


def _defined_test_problems(tests_dir: Path, file_name: str, names: tuple[str, ...]) -> list[str]:
    """Every name must be a test method of a class in `file_name` and carry no skip / expectedFailure decorator."""
    path = tests_dir / file_name
    if not path.is_file():
        return [f"{file_name}: guard test file is missing"]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods[item.name] = item
    problems = []
    for name in names:
        method = methods.get(name)
        if method is None:
            problems.append(f"{file_name}: guard test {name} is not defined")
            continue
        for decorator in method.decorator_list:
            text = ast.unparse(decorator)
            if re.search(r"\b(skip\w*|expectedFailure)\b", text):
                problems.append(f"{file_name}: guard test {name} is disabled by @{text}")
    return problems


def gate_problems(table: object, tests_dir: Path) -> list[str]:
    """What is wrong with the owner-footage switch in `table`, given the guard tests under `tests_dir`."""
    if not isinstance(table, dict) or not isinstance(table.get("ownerFootage"), dict):
        return ["venues.json has no ownerFootage object"]
    gate = table["ownerFootage"]
    problems = []
    switch = gate.get("cleanupClassGone")
    if not isinstance(switch, bool):
        problems.append(f"ownerFootage.cleanupClassGone must be a JSON boolean (the runner casts it with [bool]); got {switch!r}")
    if gate.get("refusal") != REFUSAL_TOKEN:
        problems.append(f"ownerFootage.refusal must stay {REFUSAL_TOKEN} (the token the closed gate refuses with); got {gate.get('refusal')!r}")
    if switch is True:
        for file_name, names in GUARD_TESTS.items():
            problems += _defined_test_problems(tests_dir, file_name, names)
    return problems


# ---- the route's own delete surface --------------------------------------------------------------

ROUTE_PS_SOURCES = (
    "um-run.ps1",
    "UmRunDrop.psm1",
    "gui-smoke-length-gate.ps1",
    "run-release-gui-smoke.ps1",
)
ROUTE_PS_DIRECTORIES = ("dual-venue",)
ROUTE_PY_SOURCES = ("make-contact-sheet.py",)

PS_PRIMITIVE = re.compile(
    r"\bRemove-Item\b|\bri\b|\brm\b|\bdel\b|\berase\b|\brd\b|\brmdir\b|\bClear-Item\b|\bClear-Content\b|\.Delete\(|\bMove-Item\b|"
    r"\bRename-Item\b|\bmi\b|\bmv\b|\bren\b|\[(?:System\.)?IO\.(?:File|Directory)\]::(?:Move|Replace|Delete)\(|\bMoveFileEx|\bDeleteFile|"
    r"\bSetFileInformationByHandle\b|\brmtree\b|\bHardLink\b|\bmklink\b|\bfsutil\b",
    re.IGNORECASE,
)
PY_PRIMITIVE = re.compile(r"\bos\.(?:remove|unlink|rename|replace)\(|\bshutil\.(?:move|rmtree)\(|\.unlink\(|\.rename\(")

# (file name, source line, comments removed, whitespace trimmed) -> why its target cannot be a name of owner footage.
# The owner clip is never an argument to any of these sources: the leg addresses a clip by consented ID, the job (under
# tools/profiling/bachelor, censused by test_owner_footage_delete_class.py) resolves it on the venue, and um-run.ps1 submits
# NO side file for a dual-venue leg (Invoke-VenueLeg.ps1 passes -ScriptPath/-JobId only). Everything below lives in the
# venue AGENT SHARE's inbox/outbox (job scripts and claim metadata) or in the run's own output directory.
ROUTE_DELETE_ALLOWED = {
    ("DualVenueRunner.psm1", "Remove-Item -LiteralPath `$scratch -Force"):
        "the health-probe job text: removes the 4 MiB `dve-health-<guid>.bin` the probe itself wrote under the agent root one line earlier.",
    ("um-run.ps1", "Move-Item -LiteralPath $jobFile -Destination $retractedTmp -ErrorAction Stop"):
        "retraction: renames this submission's own job script (inbox\\<id>.job.ps1) aside, no -Force; a job script is never footage.",
    ("um-run.ps1", "Remove-Item -LiteralPath $retractedTmp -Force -ErrorAction Stop"):
        "removes the `<id>.<guid>.retracted.tmp` job-script copy the line above just made.",
    ("um-run.ps1", "Remove-Item -LiteralPath $metaFinal -Force -ErrorAction Stop"):
        "retraction: removes this submission's own claim metadata (inbox\\<id>.meta.json), only when its nonce is this submission's.",
    ("UmRunDrop.psm1", "Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue"):
        "removes the `.umrun-clock-probe.<guid>` empty file the clock probe created in the inbox a moment earlier.",
    ("UmRunDrop.psm1", "Move-Item -LiteralPath $metaTmp -Destination $metaFinal -ErrorAction Stop"):
        "publishes the claim metadata temp file written one step earlier; no -Force, so it never replaces anything.",
    ("UmRunDrop.psm1", "if (Test-Path -LiteralPath $metaTmp) { Remove-Item -LiteralPath $metaTmp -Force -ErrorAction SilentlyContinue }"):
        "removes this call's own claim-metadata temp file.",
    ("UmRunDrop.psm1", "Move-Item -LiteralPath $part -Destination $destination -ErrorAction Stop"):
        "publishes a share-side `.sidepart` this call wrote and hash-verified into the inbox, no -Force (a dual-venue leg submits no side file).",
    ("UmRunDrop.psm1", "if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue }"):
        "removes the same share-side `.sidepart` copy this call made when its publish failed.",
    ("UmRunDrop.psm1", "if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }"):
        "removes this call's own job-script temp file in the inbox (two sites share the line).",
    ("UmRunDrop.psm1", "Move-Item -LiteralPath $tmp -Destination $final -ErrorAction Stop"):
        "publishes the job script into the inbox, no -Force: a job is never replaced.",
    ("UmRunDrop.psm1", "Remove-Item -LiteralPath $metaFinal -Force -ErrorAction Stop"):
        "rollback: removes this submission's own claim metadata, only when the nonce read back is its own.",
    ("gui-smoke-length-gate.ps1", "Move-Item -LiteralPath $Path -Destination $aside -ErrorAction Stop"):
        "renames a stale RESULT/receipt file at the launcher's -Output path to STALE-<utc>-<name> (never deletes it); the leg's -Output is a "
        "result.json under the job's own artifact directory, never the -Input clip view (playback-attr-3-cuda-job.ps1 builds both).",
    ("gui-smoke-length-gate.ps1", "Move-Item -LiteralPath $Path -Destination $target -ErrorAction Stop"):
        "quarantine rename of a rejected receipt/screenshot to <name>.INVALID<ext> in its own directory; same output-only paths.",
    ("New-VenueFlavorPair.ps1", "Remove-Item -LiteralPath (Join-Path $OutDir $markerName) -Force"):
        "removes the one fixed-name file $markerName (= .pair-in-progress.json, the attempt marker look-flavor-diff.py created in -OutDir for this run), "
        "non-recursive and without a wildcard, on the line after the pair record (or, second site sharing the line, the trio record) is written; a footage "
        "name cannot equal it, and the pair's -OutDir holds only derived sheets (never a clip). test_look_flavor_pair_hardening pins that the marker is gone "
        "after a finished pair or trio and kept after a crash.",
    ("VenueChain.psm1", "[IO.File]::Delete($Path)"):
        "VENUE-CHAIN-RUNNER-1: releases or breaks the venue claim file <ClaimDir>\\<venue>.claim.json (default under the board's "
        ".claude-state\\dual-venue\\claims, never a venue share). $Path is built only by Get-VenueClaimPath from a venue name matching "
        "^[a-z0-9][a-z0-9-]{0,62}$ plus the fixed suffix .claim.json, and the delete runs only after the open handle re-read the claim's own JSON "
        "(its nonce, or the exact stale bytes judged); a footage name cannot equal it. test_venue_chain_runner pins release and stale-break.",
}

TRAILING_COMMENT = re.compile(r"\s+#\s.*$")


def route_sources(profiling: Path = PROFILING) -> list[Path]:
    found = [profiling / name for name in ROUTE_PS_SOURCES]
    for directory in ROUTE_PS_DIRECTORIES:
        for suffix in ("*.ps1", "*.psm1"):
            found += sorted((profiling / directory).glob(suffix))
    found += [profiling / name for name in ROUTE_PY_SOURCES]
    return found


def route_hits(sources: list[Path]) -> set[tuple[str, str]]:
    hits = set()
    for path in sources:
        pattern = PY_PRIMITIVE if path.suffix.lower() == ".py" else PS_PRIMITIVE
        for raw in path.read_bytes().decode("utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = TRAILING_COMMENT.sub("", line)
            if pattern.search(line):
                hits.add((path.name, line))
    return hits


class GateBindsToItsGuardsTests(unittest.TestCase):
    def test_the_tracked_switch_is_open_only_while_every_named_guard_test_exists(self) -> None:
        table = json.loads(VENUES.read_text(encoding="utf-8"))
        self.assertEqual(gate_problems(table, HYGIENE), [])

    def test_the_tracked_switch_is_open_now(self) -> None:
        # The reviewed state after DVE-OWNER-FOOTAGE-GATE-1. Closing it again is a legitimate act (a new owner-footage defect), but
        # it must be a deliberate edit of this line alongside the evidence for it, not an accident.
        table = json.loads(VENUES.read_text(encoding="utf-8"))
        self.assertIs(table["ownerFootage"]["cleanupClassGone"], True)

    def test_every_guard_file_and_name_is_listed_once(self) -> None:
        for file_name, names in GUARD_TESTS.items():
            self.assertTrue((HYGIENE / file_name).is_file(), file_name)
            self.assertEqual(len(names), len(set(names)), file_name)

    def test_the_closed_state_needs_no_guard_but_keeps_its_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closed = {"ownerFootage": {"cleanupClassGone": False, "refusal": REFUSAL_TOKEN}}
            self.assertEqual(gate_problems(closed, Path(tmp)), [])
            self.assertTrue(gate_problems({"ownerFootage": {"cleanupClassGone": False, "refusal": "OTHER"}}, Path(tmp)))

    def test_the_runner_still_refuses_with_the_token_when_the_switch_is_off(self) -> None:
        # The closed case is a real code path (tests flip the switch in a temp table); keep both spellings tied to one token.
        runner = (PROFILING / "dual-venue" / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertIn(f"$refusal = '{REFUSAL_TOKEN}'", runner)
        self.assertIn("if (-not $cleanupGone) { return (& $refuse $refusal) }", runner)

    # ---- mutations: each of these must be caught ---------------------------------------------

    def _copy_guards(self, tmp: str) -> Path:
        target = Path(tmp)
        for file_name in GUARD_TESTS:
            shutil.copyfile(HYGIENE / file_name, target / file_name)
        return target

    def test_mutation_a_deleted_guard_test_keeps_the_gate_from_staying_open(self) -> None:
        open_table = {"ownerFootage": {"cleanupClassGone": True, "refusal": REFUSAL_TOKEN}}
        with tempfile.TemporaryDirectory() as tmp:
            tests = self._copy_guards(tmp)
            self.assertEqual(gate_problems(open_table, tests), [], "the unmutated copy must pass")
            victim = "test_owner_footage_no_hardlink_class.py"
            name = GUARD_TESTS[victim][0]
            text = (tests / victim).read_text(encoding="utf-8")
            self.assertEqual(text.count(f"def {name}("), 1)
            (tests / victim).write_text(text.replace(f"def {name}(", f"def renamed_{name}("), encoding="utf-8")
            self.assertTrue(any(name in problem and "not defined" in problem for problem in gate_problems(open_table, tests)))

    def test_mutation_a_missing_guard_file_keeps_the_gate_from_staying_open(self) -> None:
        open_table = {"ownerFootage": {"cleanupClassGone": True, "refusal": REFUSAL_TOKEN}}
        with tempfile.TemporaryDirectory() as tmp:
            tests = self._copy_guards(tmp)
            (tests / "test_owner_footage_delete_class.py").unlink()
            self.assertTrue(any("missing" in problem for problem in gate_problems(open_table, tests)))

    def test_mutation_a_skipped_guard_test_keeps_the_gate_from_staying_open(self) -> None:
        open_table = {"ownerFootage": {"cleanupClassGone": True, "refusal": REFUSAL_TOKEN}}
        for decorator in ("@unittest.skip('x')", "@unittest.skipIf(True, 'x')", "@unittest.expectedFailure"):
            with tempfile.TemporaryDirectory() as tmp:
                tests = self._copy_guards(tmp)
                victim = "test_owner_footage_delete_class.py"
                name = GUARD_TESTS[victim][0]
                text = (tests / victim).read_text(encoding="utf-8")
                marker = f"    def {name}("
                self.assertEqual(text.count(marker), 1)
                (tests / victim).write_text(text.replace(marker, f"    {decorator}\n{marker}"), encoding="utf-8")
                self.assertTrue(any(name in problem and "disabled" in problem for problem in gate_problems(open_table, tests)), decorator)

    def test_mutation_a_string_or_missing_switch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("true", "false", 1, None):
                table = {"ownerFootage": {"cleanupClassGone": bad, "refusal": REFUSAL_TOKEN}}
                self.assertTrue(any("JSON boolean" in p for p in gate_problems(table, Path(tmp))), repr(bad))
            self.assertTrue(gate_problems({"ownerFootage": {"refusal": REFUSAL_TOKEN}}, Path(tmp)))
            self.assertTrue(gate_problems({}, Path(tmp)))


class RouteDeleteSurfaceTests(unittest.TestCase):
    def test_every_route_source_exists(self) -> None:
        sources = route_sources()
        for path in sources:
            self.assertTrue(path.is_file(), str(path))
        names = {path.name for path in sources}
        for required in ("Invoke-VenueLeg.ps1", "DualVenueRunner.psm1", "New-VenueSheetPair.ps1", "um-run.ps1", "UmRunDrop.psm1"):
            self.assertIn(required, names)

    def test_every_delete_or_move_primitive_on_the_owner_leg_route_is_listed_with_a_reason(self) -> None:
        unexplained = sorted(route_hits(route_sources()) - set(ROUTE_DELETE_ALLOWED))
        self.assertEqual(
            unexplained, [],
            "a delete / rename / move / link primitive appeared in a source an owner-clip dual-venue leg runs. "
            "A pathname is not an identity: use the proof-taking helpers (tools/profiling/bachelor/AttrCudaArtifacts.psm1) "
            "or add the exact line to ROUTE_DELETE_ALLOWED with the reason its target cannot be a name of owner footage.")

    def test_every_listed_route_primitive_still_matches_a_line(self) -> None:
        stale = sorted(set(ROUTE_DELETE_ALLOWED) - route_hits(route_sources()))
        self.assertEqual(stale, [], "an exception no longer matches any line: delete it from ROUTE_DELETE_ALLOWED")

    def test_no_route_source_creates_a_link_or_hard_link(self) -> None:
        for path in route_sources():
            for raw in path.read_bytes().decode("utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if line.startswith("#"):
                    continue
                self.assertNotRegex(line, r"(?i)\bHardLink\b|\bmklink\b|\bfsutil\b|\bCreateHardLink", f"{path.name}: {line}")

    def test_the_leg_submits_no_side_file(self) -> None:
        # The staged-copy path (UmRunDrop's .sidepart) is for packages; an owner leg must never hand um-run a side file, or the clip's
        # bytes would be copied through a share the leg does not control.
        for path in (PROFILING / "dual-venue").glob("*.ps*1"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("-SideFile", text, path.name)

    # ---- mutations ----------------------------------------------------------------------------

    def test_mutation_a_new_delete_in_a_route_source_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profiling = Path(tmp) / "profiling"
            shutil.copytree(PROFILING / "dual-venue", profiling / "dual-venue")
            for name in ROUTE_PS_SOURCES + ROUTE_PY_SOURCES:
                shutil.copyfile(PROFILING / name, profiling / name)
            clean = route_hits(route_sources(profiling))
            self.assertEqual(sorted(clean - set(ROUTE_DELETE_ALLOWED)), [], "the unmutated copy must pass")
            for payload in (
                "Remove-Item -LiteralPath $clip -Force",
                "ri $clip",
                "[IO.File]::Delete($clip)",
                "Move-Item -LiteralPath $clip -Destination $elsewhere",
                "cmd /c mklink /H $a $b",
            ):
                target = profiling / "dual-venue" / "Invoke-VenueLeg.ps1"
                original = target.read_bytes()
                try:
                    target.write_bytes(original + b"\n" + payload.encode("utf-8") + b"\n")
                    caught = route_hits(route_sources(profiling)) - set(ROUTE_DELETE_ALLOWED)
                    self.assertIn(("Invoke-VenueLeg.ps1", payload), caught, payload)
                finally:
                    target.write_bytes(original)

    def test_mutation_a_deleted_primitive_makes_its_exception_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profiling = Path(tmp) / "profiling"
            shutil.copytree(PROFILING / "dual-venue", profiling / "dual-venue")
            for name in ROUTE_PS_SOURCES + ROUTE_PY_SOURCES:
                shutil.copyfile(PROFILING / name, profiling / name)
            target = profiling / "um-run.ps1"
            text = target.read_text(encoding="utf-8")
            line = "Remove-Item -LiteralPath $retractedTmp -Force -ErrorAction Stop"
            self.assertEqual(text.count(line), 1)
            target.write_text(text.replace(line, "# gone"), encoding="utf-8")
            stale = set(ROUTE_DELETE_ALLOWED) - route_hits(route_sources(profiling))
            self.assertIn(("um-run.ps1", line), stale)

    def test_the_scan_ignores_comment_lines_and_trailing_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ps1"
            path.write_text("# Remove-Item -Recurse $x\n$a = 1   # Remove-Item $b\n", encoding="utf-8")
            self.assertEqual(route_hits([path]), set())


if __name__ == "__main__":
    unittest.main()
