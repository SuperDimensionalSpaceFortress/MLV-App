"""FLAVOR-PAIR-HARDEN-1: the hardening Fable asked for after PR #306 (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1) on the Classic | Cinematic pair
(tools/profiling/look-flavor-diff.py, tools/profiling/dual-venue/New-VenueFlavorPair.ps1) and its cooldown gate (Wait-VenueQuiet.ps1).

  a  the pair driver stages OUTSIDE -OutDir, so "nothing of this attempt was written there" is true
  b  an attempt marker (.pair-in-progress.json) names a half record; a retry gets a typed, recoverable refusal (exit 17), not a silent block
  c  Wait-VenueQuiet.ps1 -AgentShare / -WorkDir are bound to the named venue
  d  one deadline for the whole wait        e  a transient inbox read error is retried inside it        f  usage exit code, composer race

Synthetic only: no owner footage, no venue, no share is touched. The driver is exercised on a COPY of the dual-venue tree whose module has its two evidence
readers (Test-DvReceiptValid, Read-DvContactFrames) replaced by stubs -- the mutation-copy idiom of test_dual_venue_evidence.py -- so the staging, marker and
record code runs for real without a committed-consent repo. The gate runs on a copy whose um-run.ps1 is a stub and whose venue table points at temp dirs.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from . import test_look_flavor_pair as base
from .synthetic_mlv import MLV_EXTENSION

ROOT = base.ROOT
DV = base.DV
PWSH = base.PWSH
MARKER = ".pair-in-progress.json"
requires_windows_pwsh = base.requires_windows_pwsh
requires_imaging = base.requires_imaging


def load_tool():
    spec = importlib.util.spec_from_file_location("look_flavor_diff_under_test", base.TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tree_hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# b + f: the composer ----------------------------------------------------------------------------------------------------------------------------
@requires_imaging
class ComposerAttemptMarkerTests(base.FlavorDiffHarness):
    def argv(self, out: Path, extra=(), seed: int = 1) -> list:
        self.runs += 1
        stage = self.tmp / f"stage{self.runs}" / ".claude-state" / "stage"
        cla, cla_l = self.make_side(stage, "classic", {0: base.tile(seed), 1: base.tile(seed + 1)}, None)
        cin, cin_l = self.make_side(stage, "cinematic", {0: base.tile(seed + 2), 1: base.tile(seed + 3)}, None)
        sliders = {}
        for side, doc in (("classic", base.CLASSIC), ("cinematic", base.CINEMATIC)):
            sliders[side] = stage / f"sliders-{side}.json"
            sliders[side].write_text(json.dumps(doc), encoding="utf-8")
        return ["--classic-frames", str(cla), "--classic-listed", str(cla_l), "--classic-sliders", str(sliders["classic"]),
                "--classic-flavor-reported", "classic", "--classic-receipt-id", "r-classic",
                "--cinematic-frames", str(cin), "--cinematic-listed", str(cin_l), "--cinematic-sliders", str(sliders["cinematic"]),
                "--cinematic-flavor-reported", "cinematic", "--cinematic-receipt-id", "r-cinematic",
                "--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab", "--out-dir", str(out), *extra]

    def compose(self, out: Path, *extra, seed: int = 1) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(base.TOOL), *self.argv(out, extra, seed)], capture_output=True, text=True, timeout=300)

    def out_dir(self) -> Path:
        return self.tmp / "shared" / ".claude-state" / "pair"

    def half_record(self) -> Path:
        """A directory a composer died in: the sheet and the marker were written, nothing after them."""
        out = self.out_dir()
        proc = self.compose(out, "--keep-marker")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in sorted(p.name for p in out.iterdir() if p.name not in ("sheet-classic-vs-cinematic.png", MARKER)):
            (out / name).unlink()
        self.assertEqual(sorted(p.name for p in out.iterdir()), [MARKER, "sheet-classic-vs-cinematic.png"])
        return out

    def test_a_finished_standalone_composition_leaves_no_marker(self) -> None:
        out = self.out_dir()
        proc = self.compose(out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((out / MARKER).exists())
        self.assertTrue((out / "metrics.json").exists())

    def test_keep_marker_leaves_the_attempt_marker_for_the_driver_to_remove_after_its_record(self) -> None:
        out = self.out_dir()
        proc = self.compose(out, "--keep-marker")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        marker = json.loads((out / MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["schema"], "mlv-app/look-flavor-diff-in-progress/v1")
        self.assertEqual(marker["classicReceiptId"], "r-classic")
        self.assertIsInstance(marker["pid"], int)

    def test_a_retry_into_a_half_record_gets_a_typed_recoverable_refusal_not_a_silent_block(self) -> None:
        out = self.half_record()
        before = tree_hashes(out)
        proc = self.compose(out, seed=11)
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.startswith("PAIR_INCOMPLETE_ATTEMPT"), proc.stderr)
        self.assertIn("--recover-incomplete", proc.stderr)
        self.assertIn("sheet-classic-vs-cinematic.png", proc.stderr, "the diagnosis names what the dead attempt left")
        self.assertEqual(tree_hashes(out), before, "the refusal changes nothing")

    def test_a_marker_with_no_outputs_is_recognised_too(self) -> None:
        out = self.out_dir()
        out.mkdir(parents=True)
        (out / MARKER).write_text('{"schema": "x"}', encoding="utf-8")
        proc = self.compose(out)
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.startswith("PAIR_INCOMPLETE_ATTEMPT"), proc.stderr)

    def test_recover_incomplete_moves_the_unrecorded_files_aside_and_composes_without_deleting_anything(self) -> None:
        out = self.half_record()
        old = tree_hashes(out)
        proc = self.compose(out, "--recover-incomplete", seed=21)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        quarantine = [p for p in out.iterdir() if p.is_dir() and p.name.startswith("incomplete-")]
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(tree_hashes(quarantine[0]), old, "the dead attempt's bytes are preserved, marker included")
        self.assertTrue((out / "metrics.json").exists() and (out / "table.md").exists())
        self.assertFalse((out / MARKER).exists(), "the finished recovery leaves no marker")

    def test_recover_incomplete_does_nothing_for_a_directory_without_a_marker(self) -> None:
        out = self.out_dir()
        out.mkdir(parents=True)
        (out / "metrics.json").write_bytes(b"earlier evidence")
        proc = self.compose(out, "--recover-incomplete")
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertEqual(tree_hashes(out), {"metrics.json": hashlib.sha256(b"earlier evidence").hexdigest()})

    def test_recover_incomplete_refuses_a_directory_that_holds_a_pair_record_and_moves_nothing(self) -> None:
        # FLAVOR-DIFF-RECOVER-RECORD-GUARD-1: the record names the sheet by hash; moving the sheet aside would leave that hash stale.
        out = self.half_record()
        (out / "flavor-pair-leg-a-vs-leg-b-bachelor.json").write_bytes(b'{"record":1}\n')
        before = tree_hashes(out)
        proc = self.compose(out, "--recover-incomplete", seed=41)
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("PAIR_RECORD_EXISTS", proc.stderr)
        self.assertEqual(tree_hashes(out), before, "nothing was moved, written or removed")
        self.assertEqual([p.name for p in out.iterdir() if p.is_dir()], [], "no incomplete-<utc> directory was made")

    def test_recover_incomplete_refuses_a_directory_that_holds_a_trio_record_and_moves_nothing(self) -> None:
        # FLAVOR-TRIO-RECOVERY-PROTECT-1: a completed trio's record names its sheet / metrics / table / rows; pair-mode recovery must not move them.
        out = self.out_dir()
        out.mkdir(parents=True)
        for name, data in (("flavor-trio-leg-a-leg-b-leg-c-bachelor.json", b'{"schema": "mlv-app/dual-venue-flavor-trio/v1"}\n'),
                           ("sheet-classic-cinematic-film.png", b"trio sheet"), ("metrics.json", b"trio metrics"), ("table.md", b"trio table"),
                           ("row-00.png", b"trio row"), (MARKER, b'{"schema": "stale"}')):
            (out / name).write_bytes(data)
        before = tree_hashes(out)
        proc = self.compose(out, "--recover-incomplete", seed=51)
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("PAIR_RECORD_EXISTS flavor-trio-leg-a-leg-b-leg-c-bachelor.json", proc.stderr)
        self.assertEqual(tree_hashes(out), before, "every file kept its path and bytes")
        self.assertEqual([p.name for p in out.iterdir() if p.is_dir()], [], "no incomplete-<utc> directory was made")

    def test_a_crash_between_the_sheet_and_the_metrics_leaves_a_marker_and_the_next_retry_is_refused_with_it(self) -> None:
        mod = load_tool()
        real = mod.write_new

        def crash(path, data):
            if Path(path).name == "metrics.json":
                raise OSError("simulated crash after the sheet")
            return real(path, data)

        mod.write_new = crash
        out = self.out_dir()
        with self.assertRaises(OSError):
            mod.main(self.argv(out))
        self.assertTrue((out / "sheet-classic-vs-cinematic.png").exists())
        self.assertFalse((out / "metrics.json").exists())
        self.assertTrue((out / MARKER).exists(), "the crash left nothing marking the attempt as incomplete")
        proc = self.compose(out, seed=31)
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)

    # f: FLAVOR-DIFF-RACE-TEST-1 -- the window between the up-front occupancy check and the exclusive create ------------------------------------
    def test_a_composer_that_passed_the_check_but_lost_the_race_writes_nothing_and_leaves_no_marker(self) -> None:
        out = self.out_dir()
        out.mkdir(parents=True)
        (out / "sheet-classic-vs-cinematic.png").write_bytes(b"the winner's sheet, created after our check")
        before = tree_hashes(out)
        mod = load_tool()
        mod.refuse_if_occupied = lambda _out, _indices: None   # the check this composer already passed, before the winner wrote
        code = mod.main(self.argv(out))
        self.assertEqual(code, 16)
        self.assertEqual(tree_hashes(out), before, "the loser changed nothing: no metrics, no table, no marker, the winner's sheet intact")


# a + b: the driver --------------------------------------------------------------------------------------------------------------------------------
STUB_READERS = r"""

# --- FLAVOR-PAIR-HARDEN-1 test stubs (appended to a COPY of the module): the two evidence readers, so the driver's staging / marker / record run for real ---
function Test-DvReceiptValid {
    param($Receipt, $RepoRoot)
    [pscustomobject]@{ status = 'ADVISORY'; legType = 'look'; evidenceDir = [string]$Receipt.evidence.localEvidenceDir; reasons = @() }
}
function Read-DvContactFrames {
    param($Receipt, [string]$EvidenceDir)
    $raw = Join-Path $EvidenceDir 'contact-sheet\raw'
    $files = @(Get-ChildItem -LiteralPath $raw -File | Sort-Object Name | ForEach-Object {
        $b = [IO.File]::ReadAllBytes($_.FullName); [pscustomobject]@{ name = $_.Name; sha256 = (Get-DvSha256OfBytes $b); bytes = $b } })
    [pscustomobject]@{ ok = $true; reasons = @(); rawDir = $raw; files = $files }
}
"""


@requires_windows_pwsh
@requires_imaging
class DriverStagingAndMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="flavor-pair-harden-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        tree = self.tmp / "tree"
        self.dv = tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("look-flavor-diff.py", "make-contact-sheet.py"):
            shutil.copyfile(ROOT / "tools" / "profiling" / name, tree / "tools" / "profiling" / name)
        with (self.dv / "DualVenueRunner.psm1").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(STUB_READERS)
        self.parent = self.tmp / "work" / ".claude-state"
        self.out = self.parent / "pair"
        self.classic, self.cinematic = self.receipt("classic", base.CLASSIC, 10), self.receipt("cinematic", base.CINEMATIC, 20)

    def receipt(self, flavor: str, sliders: dict, seed: int, tag: str = "") -> Path:
        from PIL import Image
        ev = self.tmp / f"evidence-{flavor}{tag}"
        raw = ev / "contact-sheet" / "raw"
        raw.mkdir(parents=True)
        for i in (0, 1):
            Image.fromarray(base.tile(seed + i)).save(raw / f"frame-{i:02d}.png")
            (raw / f"frame-{i:02d}.json").write_text(json.dumps({"index": i, "saved": True, "display_frame": 30 + 40 * i, "elapsed_ms": 2000.0 + 4000 * i,
                                                                 "path": f"frame-{i:02d}.png", "playback_path": True}), encoding="utf-8")
        result = json.dumps({"visualQuality": {"lookAssist": sliders}}).encode("utf-8")
        summary = json.dumps({"lookFlavorReported": flavor}).encode("utf-8")
        (ev / "result.json").write_bytes(result)
        (ev / "summary.json").write_bytes(summary)
        doc = base.synthetic_receipt(flavor)
        doc["evidence"] = {"localEvidenceDir": str(ev), "resultJsonSha256": hashlib.sha256(result).hexdigest(), "summaryJsonSha256": hashlib.sha256(summary).hexdigest()}
        path = self.tmp / f"{flavor}{tag}.receipt.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def pair(self, *extra, out: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "New-VenueFlavorPair.ps1"),
                               "-ClassicReceipt", str(self.classic), "-CinematicReceipt", str(self.cinematic), "-OutDir", str(out or self.out), *extra],
                              capture_output=True, text=True, timeout=300)

    def names(self, root: Path) -> list:
        return sorted(p.name for p in root.iterdir())

    def staging_dirs(self) -> list:
        """The .pair-staging-<guid> directories beside the out dir. The driver never deletes them (the owner-footage route guards allow no recursive delete)."""
        return [p for p in self.parent.iterdir() if p.name.startswith(".pair-staging-") and p.is_dir()]

    def test_a_composer_refusal_exit_16_leaves_nothing_of_the_attempt_in_the_out_dir_or_beside_it(self) -> None:
        # row-00.png is not in the driver's own up-front list (sheet / metrics / table / record), so this attempt reaches the composer and is refused THERE.
        self.out.mkdir(parents=True)
        (self.out / "row-00.png").write_bytes(b"earlier evidence")
        proc = self.pair()
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("nothing of this attempt was written there", proc.stdout + proc.stderr)
        self.assertEqual(self.names(self.out), ["row-00.png"], "the claim is true: no staging directory, no marker, nothing")
        self.assertEqual(self.names(self.parent), sorted(["pair"] + [p.name for p in self.staging_dirs()]), "beside it there is only the out dir and the inert staging directories")
        self.assertEqual(len(self.staging_dirs()), 1, "the attempt's own staging directory is kept, not deleted (and it is not inside the out dir)")

    def test_a_finished_pair_leaves_the_record_and_composer_outputs_only(self) -> None:
        proc = self.pair()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = self.names(self.out)
        self.assertIn("flavor-pair-leg-classic-vs-leg-cinematic-bachelor.json", names)
        self.assertIn("sheet-classic-vs-cinematic.png", names)
        self.assertNotIn(".pair-staging", names, "staging is not part of the pair's evidence directory")
        self.assertNotIn(MARKER, names, "the record is written, so the attempt is complete")
        self.assertEqual(len(self.staging_dirs()), 1, "the staging directory sits beside the out dir and is not deleted (the route guards allow no recursive delete)")
        self.assertEqual(self.names(self.parent), sorted(["pair", self.staging_dirs()[0].name]), "nothing else is beside the out dir")

    def test_an_inert_refusal_keeps_its_slider_files_outside_the_out_dir_and_the_message_names_them(self) -> None:
        self.cinematic = self.receipt("cinematic", base.CLASSIC, 20, tag="-inert")   # the flavor-owned sliders are identical: FLAVOR_INERT (exit 10)
        proc = self.pair()
        self.assertNotEqual(proc.returncode, 0)
        text = proc.stdout + proc.stderr
        self.assertIn("PAIR_FLAVOR_INERT", text)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()), "nothing was written into -OutDir")
        stray = [p for p in self.parent.iterdir() if p.name.startswith(".pair-staging-")]
        self.assertEqual(len(stray), 1, "the slider files the message names are kept for diagnosis")
        self.assertTrue((stray[0] / "sliders-cinematic.json").exists())
        self.assertIn(str(stray[0]), text)

    def crash_before_the_record(self) -> None:
        """A directory sitting where the record file goes: the composer finishes, the record write fails, the marker stays."""
        (self.out).mkdir(parents=True)
        (self.out / "flavor-pair-leg-classic-vs-leg-cinematic-bachelor.json").mkdir()   # (the up-front record check lists FILES only)
        proc = self.pair()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.out / "sheet-classic-vs-cinematic.png").exists())
        self.assertTrue((self.out / MARKER).exists(), "a crash between the sheet and the record leaves the attempt marker")
        (self.out / "flavor-pair-leg-classic-vs-leg-cinematic-bachelor.json").rmdir()

    def test_a_retry_after_a_crash_before_the_record_is_refused_with_the_incomplete_diagnosis(self) -> None:
        self.crash_before_the_record()
        before = tree_hashes(self.out)
        proc = self.pair()
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        self.assertIn("PAIR_INCOMPLETE_ATTEMPT", proc.stdout + proc.stderr)
        self.assertIn("-RecoverIncomplete", proc.stdout + proc.stderr)
        self.assertEqual(tree_hashes(self.out), before)

    def test_recover_incomplete_quarantines_the_dead_attempt_and_the_retry_completes(self) -> None:
        self.crash_before_the_record()
        old = tree_hashes(self.out)
        proc = self.pair("-RecoverIncomplete")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        quarantine = [p for p in self.out.iterdir() if p.is_dir() and p.name.startswith("incomplete-")]
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(tree_hashes(quarantine[0]), old)
        self.assertIn("flavor-pair-leg-classic-vs-leg-cinematic-bachelor.json", self.names(self.out))
        self.assertNotIn(MARKER, self.names(self.out))

    def test_a_marker_only_directory_is_refused_with_the_incomplete_diagnosis(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / MARKER).write_text('{"schema": "x"}', encoding="utf-8")
        proc = self.pair()
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        self.assertIn("PAIR_INCOMPLETE_ATTEMPT", proc.stdout + proc.stderr)

    def test_a_pair_record_still_wins_over_a_marker_and_recover_incomplete(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / MARKER).write_text('{"schema": "x"}', encoding="utf-8")
        (self.out / "flavor-pair-leg-old-vs-leg-old2-bachelor.json").write_bytes(b'{"record":1}\n')
        before = tree_hashes(self.out)
        for extra in ((), ("-RecoverIncomplete",)):
            proc = self.pair(*extra)
            self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
            self.assertIn("PAIR_RECORD_EXISTS", proc.stdout + proc.stderr)
            self.assertEqual(tree_hashes(self.out), before)

    # r3 blocker 3: a record that died half-written must not mask the marker --------------------------------------------------------------------
    RECORD_NAME = "flavor-pair-leg-classic-vs-leg-cinematic-bachelor.json"

    def half_written_record(self, content: bytes) -> None:
        """The state a crash leaves between the exclusive create of the record and the end of its write: outputs, the marker and a bad record."""
        self.crash_before_the_record()
        (self.out / self.RECORD_NAME).write_bytes(content)

    def test_a_marker_with_outputs_and_an_empty_or_truncated_record_is_the_incomplete_diagnosis_not_record_exists(self) -> None:
        for label, content in (("empty", b""), ("truncated", b'{"schema": "mlv-app/dual-venue-flavor-pair/v1", "venue": "bachel')):
            with self.subTest(record=label):
                if self.out.exists():
                    shutil.rmtree(self.out)
                self.half_written_record(content)
                before = tree_hashes(self.out)
                for extra in ((), ("-RecoverIncomplete",)):
                    proc = self.pair(*extra)
                    text = proc.stdout + proc.stderr
                    self.assertEqual(proc.returncode, 17, text)
                    self.assertIn("PAIR_INCOMPLETE_ATTEMPT", text)
                    self.assertNotIn("PAIR_RECORD_EXISTS", text)
                    self.assertIn(self.RECORD_NAME, text, "the diagnosis names the half record")
                    self.assertEqual(tree_hashes(self.out), before, "the half record's bytes, the sheet and the marker are all preserved")

    def test_a_complete_record_beside_a_marker_keeps_exit_16(self) -> None:
        self.half_written_record(b'{"schema": "mlv-app/dual-venue-flavor-pair/v1", "venue": "bachelor"}\n')
        before = tree_hashes(self.out)
        for extra in ((), ("-RecoverIncomplete",)):
            proc = self.pair(*extra)
            self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
            self.assertIn("PAIR_RECORD_EXISTS", proc.stdout + proc.stderr)
            self.assertEqual(tree_hashes(self.out), before)

    def test_a_half_record_with_no_marker_is_still_record_exists(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / self.RECORD_NAME).write_bytes(b"")
        proc = self.pair()
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertEqual(tree_hashes(self.out), {self.RECORD_NAME: hashlib.sha256(b"").hexdigest()})

    # FILM-TRIO-INCOMPLETE-ATTEMPT-MARKER-1: the trio carries the pair's attempt marker up to its own record --------------------------------------
    TRIO_RECORD_NAME = "flavor-trio-leg-classic-leg-cinematic-leg-film-bachelor.json"

    def trio(self, *extra) -> subprocess.CompletedProcess:
        if not hasattr(self, "film"):
            self.film = self.receipt("film", base.FILM, 30)
        return self.pair("-FilmReceipt", str(self.film), *extra)

    def test_a_finished_trio_leaves_the_record_and_no_marker(self) -> None:
        proc = self.trio()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = self.names(self.out)
        self.assertIn(self.TRIO_RECORD_NAME, names)
        self.assertIn("sheet-classic-cinematic-film.png", names)
        self.assertNotIn(MARKER, names, "the trio record is written, so the attempt is complete")

    def test_an_interrupted_trio_is_the_incomplete_attempt_17_not_record_exists_16(self) -> None:
        # A directory where the trio record goes: the composer finishes, the record's exclusive create fails -- the trio died before its record.
        self.out.mkdir(parents=True)
        (self.out / self.TRIO_RECORD_NAME).mkdir()
        proc = self.trio()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.out / "sheet-classic-cinematic-film.png").exists())
        self.assertTrue((self.out / MARKER).exists(), "a trio that dies before its record leaves the attempt marker, as the pair does")
        (self.out / self.TRIO_RECORD_NAME).rmdir()
        for label, content in (("no record", None), ("empty record", b""), ("truncated record", b'{"schema": "mlv-app/dual-venue-flavor-trio/v1", "ven')):
            with self.subTest(state=label):
                if content is not None:
                    (self.out / self.TRIO_RECORD_NAME).write_bytes(content)   # the state an interrupted record write leaves
                before = tree_hashes(self.out)
                proc = self.trio()
                text = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 17, text)
                self.assertIn("PAIR_INCOMPLETE_ATTEMPT", text)
                self.assertNotIn("PAIR_RECORD_EXISTS", text)
                self.assertNotIn("-RecoverIncomplete to MOVE", text, "the trio is not recoverable in place; the diagnosis must not offer it")
                if content is not None:
                    self.assertIn(self.TRIO_RECORD_NAME, text, "the diagnosis names the half record")
                self.assertEqual(tree_hashes(self.out), before, "the dead trio's bytes are preserved")

    def test_recover_incomplete_never_moves_a_completed_trios_evidence_trio(self) -> None:
        # FLAVOR-TRIO-RECOVERY-PROTECT-1: pair mode (no -FilmReceipt) with -RecoverIncomplete into a finished trio's directory plus a stale marker.
        proc = self.trio()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (self.out / MARKER).write_text('{"schema": "stale"}', encoding="utf-8")
        before = tree_hashes(self.out)
        self.assertIn(self.TRIO_RECORD_NAME, before)
        proc = self.pair("-RecoverIncomplete")
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("PAIR_RECORD_EXISTS " + self.TRIO_RECORD_NAME, proc.stdout + proc.stderr)
        self.assertEqual(tree_hashes(self.out), before, "every file kept its path and bytes")
        self.assertEqual([p.name for p in self.out.iterdir() if p.is_dir()], [], "no incomplete-<utc> directory was made")

    # r3 blocker 1: the marker-delete exception never runs inside an owner-footage root --------------------------------------------------------
    def test_an_out_dir_that_holds_or_sits_under_owner_footage_is_refused_before_anything_is_written_or_deleted(self) -> None:
        clip = b"unrelated owner clip bytes"
        cases = {
            "clip in the out dir": (self.out / ("owner-unrelated" + MLV_EXTENSION)),
            "continuation part in the out dir": (self.out / "owner-unrelated.M00"),
            "clip in a subdirectory of the out dir": (self.out / "sub" / ("owner-unrelated" + MLV_EXTENSION)),
            "clip in the parent .claude-state directory": (self.parent / ("owner-unrelated" + MLV_EXTENSION)),
        }
        for label, path in cases.items():
            with self.subTest(case=label):
                if self.parent.exists():
                    shutil.rmtree(self.parent)
                self.out.mkdir(parents=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(clip)
                (self.out / MARKER).write_text('{"schema": "x"}', encoding="utf-8")   # (the marker a finished pair would delete)
                before = tree_hashes(self.parent)
                proc = self.pair()
                text = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 18, text)
                self.assertIn("PAIR_OUTDIR_HOLDS_OWNER_FOOTAGE", text)
                self.assertEqual(tree_hashes(self.parent), before, "nothing was written, staged or deleted; the clip is byte-identical")
                self.assertEqual(path.read_bytes(), clip)


# c + d + e + f: the cooldown gate ----------------------------------------------------------------------------------------------------------------
STUB_UM_RUN = r"""param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec)
# test stub for tools\profiling\um-run.ps1: records the share it was aimed at, optionally leaves a foreign queued job behind, returns canned samples
Add-Content -LiteralPath $env:UMSTUB_LOG -Value "$JobId $AgentShare"
if ($env:UMSTUB_FOREIGN -eq '1') { New-Item -ItemType Directory -Force -Path (Join-Path $AgentShare 'inbox') | Out-Null; Set-Content -LiteralPath (Join-Path $AgentShare 'inbox\foreign.job.ps1') -Value '# queued by someone else' }
$probe = [ordered]@{ schema = 'mlv-app/venue-quiet-probe/v1'; host = $(if ($env:UMSTUB_HOST) { $env:UMSTUB_HOST } else { 'STUB' }); samples = @($env:UMSTUB_SAMPLES | ConvertFrom-Json); top = @() }
[pscustomobject]@{ exitCode = 0; stdout = 'VENUE_QUIET=' + ($probe | ConvertTo-Json -Compress -Depth 4) }
"""


@requires_windows_pwsh
class VenueQuietGateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-quiet-harden-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        tree = self.tmp / "tree"
        self.dv = tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        (tree / "tools" / "profiling" / "um-run.ps1").write_text(STUB_UM_RUN, encoding="utf-8")
        self.share = {"bachelor": self.tmp / "share-bachelor", "ultra-magnus": self.tmp / "share-ultra-magnus"}
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        for name, share in self.share.items():
            (share / "inbox").mkdir(parents=True)
            (share / "running").mkdir()
            table["venues"][name]["agentShare"] = str(share)
            table["venues"][name]["agentRoot"] = str(share) + "-root"
        (self.dv / "venues.json").write_text(json.dumps(table), encoding="utf-8")
        self.work = self.tmp / "work"
        self.log = self.tmp / "um.log"
        self.log.write_text("", encoding="utf-8")

    def env(self, samples="[5, 5, 5]", foreign=False, host="BACHELOR") -> dict:
        return dict(os.environ, UMSTUB_LOG=str(self.log), UMSTUB_SAMPLES=samples, UMSTUB_FOREIGN="1" if foreign else "0", UMSTUB_HOST=host)

    def gate_args(self, *extra, venue="bachelor", work: Path | None = None) -> list:
        return [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", venue,
                "-WorkDir", str(work or self.work), *extra]

    def run_gate(self, *extra, venue="bachelor", work: Path | None = None, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(self.gate_args(*extra, venue=venue, work=work), capture_output=True, text=True, timeout=180, env=env or self.env())

    # c --------------------------------------------------------------------------------------------------------------------------------------------
    def test_another_venues_share_is_refused_for_the_named_venue_even_when_acknowledged(self) -> None:
        for extra in ((), ("-AllowShareOverride",)):
            with self.subTest(extra=extra):
                proc = self.run_gate("-AgentShare", str(self.share["ultra-magnus"]), *extra)
                text = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 2, text)
                self.assertIn("SHARE_VENUE_MISMATCH", text)
                self.assertNotIn("QUIET mean", text, "a refused share must never print a verdict for the named venue")
                self.assertEqual(self.log.read_text(encoding="utf-8"), "", "no probe was submitted")

    def test_a_share_that_is_no_venues_own_needs_an_explicit_acknowledgement(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "inbox").mkdir(parents=True)
        proc = self.run_gate("-AgentShare", str(elsewhere))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("SHARE_NOT_VENUE_OWN", proc.stdout + proc.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "")
        proc = self.run_gate("-AgentShare", str(elsewhere), "-AllowShareOverride")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("QUIET mean=5.0%", proc.stdout)

    def test_the_named_venues_own_share_is_accepted_however_it_is_spelled(self) -> None:
        own = str(self.share["bachelor"])
        for spelling in (own, own.upper(), own + "\\", own.replace("\\", "/")):
            with self.subTest(spelling=spelling):
                proc = self.run_gate("-AgentShare", spelling)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("QUIET mean=5.0%", proc.stdout)

    def test_a_work_dir_inside_a_venue_share_is_refused(self) -> None:
        proc = self.run_gate(work=self.share["ultra-magnus"] / "inbox" / "w")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("WORKDIR_IN_VENUE_SHARE", proc.stdout + proc.stderr)
        self.assertFalse((self.share["ultra-magnus"] / "inbox" / "w").exists())

    # r3 blocker 2 (WAIT-VENUE-QUIET-HOST-ECHO-1): the host that answered must be the venue's own ----------------------------------------------------
    def test_a_probe_answered_by_another_host_is_a_host_mismatch_never_a_quiet_verdict(self) -> None:
        # an acknowledged alias that is not either venue's configured storage, but is served by the OTHER venue's host
        alias = self.tmp / "alias-of-ultra-magnus"
        (alias / "inbox").mkdir(parents=True)
        (alias / "running").mkdir()
        proc = self.run_gate("-AgentShare", str(alias), "-AllowShareOverride", env=self.env("[5, 5, 5]", host="ULTRA-MAGNUS"))
        text = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 5, text)
        self.assertIn("HOST_MISMATCH", text)
        self.assertNotIn("QUIET mean", text)
        self.assertNotIn("COOLDOWN_UNMET", text)

    def test_the_probe_host_is_compared_case_insensitively_and_a_missing_host_is_a_mismatch(self) -> None:
        proc = self.run_gate(env=self.env(host="bachelor"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("QUIET mean=5.0%", proc.stdout)
        proc = self.run_gate(env=self.env(host=""))   # (the stub falls back to host 'STUB')
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)

    def test_a_gate_log_inside_any_venue_share_is_refused_before_anything_is_created_or_submitted(self) -> None:
        for venue in ("bachelor", "ultra-magnus"):
            with self.subTest(venue=venue):
                target = self.share[venue] / "inbox" / "gate.log"
                proc = self.run_gate("-GateLog", str(target))
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("GATELOG_IN_VENUE_SHARE", proc.stdout + proc.stderr)
                self.assertFalse(target.exists())
                self.assertEqual(self.log.read_text(encoding="utf-8"), "", "no probe was submitted")

    def test_a_gate_log_outside_the_work_dir_and_any_claude_state_directory_is_refused(self) -> None:
        stray = self.tmp / "elsewhere" / "gate.log"
        proc = self.run_gate("-GateLog", str(stray))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("GATELOG_OUT_OF_SCOPE", proc.stdout + proc.stderr)
        self.assertFalse(stray.parent.exists())
        for allowed in (self.work / "gate.log", self.tmp / ".claude-state" / "gate.log"):
            with self.subTest(allowed=str(allowed.relative_to(self.tmp))):
                allowed.parent.mkdir(parents=True, exist_ok=True)
                proc = self.run_gate("-GateLog", str(allowed))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("QUIET mean=5.0%", allowed.read_text(encoding="utf-8"))

    # d --------------------------------------------------------------------------------------------------------------------------------------------
    def test_max_wait_sec_bounds_the_whole_wait_even_when_every_reprobe_meets_a_busy_queue(self) -> None:
        # Probe 1 leaves a foreign job in the inbox (the venue is busy again); the old code then gave the SECOND queue gate a fresh MaxGateSec clock.
        started = time.monotonic()
        proc = self.run_gate("-MaxWaitSec", "3", "-RecheckSec", "1", "-MaxGateSec", "25", "-GatePollSec", "1", env=self.env("[90, 90, 90]", foreign=True))
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.count("PROBE "), 1)
        self.assertLess(elapsed, 18, "the second gate got its own 25 s clock: MaxWaitSec 3 did not bound the wait")
        waited = [int(line.rsplit("waitedSec=", 1)[1]) for line in proc.stdout.splitlines() if line.startswith("GATE_BUSY quiet-probe waitedSec=")]
        self.assertEqual(len(waited), 1, proc.stdout)
        self.assertLessEqual(waited[0], 3 + 2)

    def test_the_gate_still_honours_a_tighter_max_gate_sec(self) -> None:
        (self.share["bachelor"] / "inbox" / "earlier.job.ps1").write_text("# queued", encoding="utf-8")
        proc = self.run_gate("-MaxGateSec", "0", "-GatePollSec", "1")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("GATE_BUSY quiet-probe", proc.stdout)

    # e --------------------------------------------------------------------------------------------------------------------------------------------
    def test_one_transient_inbox_read_failure_is_retried_inside_the_deadline_not_exit_4(self) -> None:
        gone = self.tmp / "share-appears-late"
        gate_log = self.work / "gate.log"
        proc = subprocess.Popen(self.gate_args("-AgentShare", str(gone), "-AllowShareOverride", "-GateLog", str(gate_log), "-ReadBackoffSec", "1", "-MaxWaitSec", "120"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env())

        def share_comes_back() -> None:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and proc.poll() is None:
                if gate_log.exists() and "GATE_READ_RETRY" in gate_log.read_text(encoding="utf-8"):
                    (gone / "inbox").mkdir(parents=True, exist_ok=True)
                    (gone / "running").mkdir(exist_ok=True)
                    return
                time.sleep(0.2)

        thread = threading.Thread(target=share_comes_back)
        thread.start()
        out, err = proc.communicate(timeout=170)
        thread.join()
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("GATE_READ_RETRY", out)
        self.assertNotIn("GATE_UNREADABLE", out)
        self.assertIn("QUIET mean=5.0%", out)

    def test_a_share_that_stays_unreadable_is_retried_a_bounded_number_of_times_then_exits_4(self) -> None:
        proc = self.run_gate("-AgentShare", str(self.tmp / "no-such-share"), "-AllowShareOverride", "-ReadBackoffSec", "0")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 4, out)
        self.assertEqual(out.count("GATE_READ_RETRY"), 2, "three reads in all: two retries")
        self.assertIn("GATE_UNREADABLE quiet-probe bachelor", out)
        self.assertIn("DECISION UNKNOWN mean=UNKNOWN", out)
        self.assertNotIn("queued=0 running=0", out)
        self.assertNotIn("PROBE ", out)

    # f --------------------------------------------------------------------------------------------------------------------------------------------
    def test_a_malformed_samples_json_is_a_usage_error_exit_2(self) -> None:
        for bad in ("abc", "{not json"):
            with self.subTest(samples=bad):
                proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "Wait-VenueQuiet.ps1"), "-SamplesJson", bad],
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("DECISION", proc.stdout)


if __name__ == "__main__":
    unittest.main()
