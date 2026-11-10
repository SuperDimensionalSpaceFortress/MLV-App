"""LANE-LOCAL-STAGE-CHAIN-DEADLINE-BYPASS-1 r2 (Dot 791-I1): the heavy-slot deadline is OBSERVED at run time, its boundaries are named, and the
tracked callers of the attr3 assembler are read from the PowerShell syntax tree.

r1 stopped at the assembler's own gate and at textual caller checks. Here:

 1. StageChainPropagationTests -- the real stage-chain SHAPE (chain -> stage-one -> assembler -> build guard) is run with a stub assembler and a
    stub guard; the stub at every hop records the MLV_HEAVY_SLOT_DEADLINE_UTC it received. The tracked Invoke-VenueStageChain.ps1 and a run-dir
    copy that sets the variable hand the guard the deadline unchanged; a copy that sets nothing reaches the REAL assembler, which ends
    STAGE_NO_HEAVY_SLOT_DEADLINE (exit 27) before it takes a path, a ticket or the guard. No DLL pair, no build, no heavy slot, no footage.
 2. ChainLayerBoundaryTests -- the deadline's boundaries, each with the LAYER that judges it and the typed reason it gives:

      boundary                         assembler (exit 27 gate)   tracked chain                              build-guard (tests/coordination)
      absent                           REFUSES STAGE_NO_HEAVY..   REFUSES invalid-deadline (exit 2)          none (60 min default) -- not its job
      blank / whitespace               REFUSES STAGE_NO_HEAVY..   REFUSES invalid-deadline (exit 2)          none (60 min default)
      malformed (not ISO-8601 UTC)     passes it on               REFUSES invalid-deadline (exit 2)          REFUSES invalid-option (exit 2)
      non-UTC offset                   passes it on               accepts, hands the child the UTC instant   accepts, same instant
      already expired                  passes it on               no attempt starts: deadline-passed (exit 3) one --wait 0m attempt, then capacity exit
      expires inside a minimum window  passes it on               accepts                                    accepts -- NOBODY REFUSES (unproved limit)
      valid                            passes it on               hands the child the round-trip instant     bounded --wait, WAIT_BOUND line

    The assembler judges only "was one given". The guard-layer rows are pinned in tests/coordination/test_build_guard.py
    (test_boundary_*), where the real guard and the heavy-slot stub live. build-guard.ps1 semantics are NOT changed by this card.
 3. TrackedAssemblerCallerAstTests -- every tracked PowerShell caller of the assembler is found and judged from [Parser]::ParseFile, so a
    comment, a string in a log line or a re-ordered line cannot fool it; widening mutations go RED.

Chain-running tests are Windows-only: the stage chain stages Windows venue builds (Bachelor, Ultra-Magnus) and its sibling chain tests
(tests/coordination/test_build_guard.py) are Windows-only too; the Windows profile of hosted CI runs them. The AST scan runs everywhere.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1"
CHAIN = ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageChain.ps1"
NIGHTLY = ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageNightly.ps1"
HEAVY_STUB = ROOT / "tests" / "coordination" / "heavy_slot_stub" / "heavy-slot.cmd"
ENV_NAME = "MLV_HEAVY_SLOT_DEADLINE_UTC"
NEEDLE = "playback-attr-3-cuda-assemble"
SHA = "0123456789abcdef0123456789abcdef01234567"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

WINDOWS_CHAIN_REASON = (
    "the venue stage chain stages Windows builds (Bachelor, Ultra-Magnus); its sibling chain tests in tests/coordination/test_build_guard.py "
    "are Windows-only too, and the Windows profile of hosted CI runs this one"
)
windows_chain_only = unittest.skipUnless(os.name == "nt" and PWSH, WINDOWS_CHAIN_REASON)

# --- stubs (ASCII, written into a temp run dir) ---------------------------------------------------------------------------------------------

STUB_GUARD = (
    "$ErrorActionPreference = 'Stop'\n"
    "Add-Content -LiteralPath $env:STUB_RECORD -Value ('who=guard hs=[' + $env:MLV_HEAVY_SLOT_DEADLINE_UTC + ']')\n"
    "exit 0\n"
)
STUB_ASSEMBLER = (
    "param([string]$SourceCommit, [string]$DllPairDir, [string]$OutDir, [string]$RepoRoot)\n"
    "Add-Content -LiteralPath $env:STUB_RECORD -Value ('who=assembler hs=[' + $env:MLV_HEAVY_SLOT_DEADLINE_UTC + ']')\n"
    "& $env:STUB_PWSH -NoProfile -NonInteractive -File $env:STUB_GUARD\n"
    "exit $LASTEXITCODE\n"
)
# The incident's stage-one shape: param(Sha, Phase), no deadline parameter anywhere; it calls whichever assembler STUB_ASSEMBLER names.
STAGE_ONE = (
    "param([Parameter(Mandatory = $true)][string]$Sha, [Parameter(Mandatory = $true)][ValidateSet('dll', 'stage')][string]$Phase)\n"
    "$ErrorActionPreference = 'Stop'\n"
    "Add-Content -LiteralPath $env:STUB_RECORD -Value ('who=stage-one phase=' + $Phase + ' hs=[' + $env:MLV_HEAVY_SLOT_DEADLINE_UTC + ']')\n"
    "if ($Phase -eq 'dll') { [Console]::Out.WriteLine('STAGE_ONE_RESULT phase=dll sha=' + $Sha + ' outcome=DLLPAIR_HARVESTED'); exit 0 }\n"
    "$out = & $env:STUB_PWSH -NoProfile -NonInteractive -File $env:STUB_ASSEMBLER -SourceCommit $Sha -DllPairDir (Join-Path $env:STUB_WORK 'no-dll-pair') "
    "-OutDir (Join-Path $env:STUB_WORK 'out') -RepoRoot $env:STUB_WORK 2>&1 | ForEach-Object { [string]$_ }\n"
    "$ax = $LASTEXITCODE\n"
    "$out | ForEach-Object { [Console]::Out.WriteLine($_) }\n"
    "Add-Content -LiteralPath $env:STUB_RECORD -Value ('who=assembler-exit code=' + $ax)\n"
    "if ($ax -ne 0) { [Console]::Out.WriteLine('STAGE_ONE_RESULT phase=stage sha=' + $Sha + ' outcome=ASSEMBLE_FAILED exit=' + $ax); exit 1 }\n"
    "[Console]::Out.WriteLine('STAGE_ONE_RESULT phase=stage sha=' + $Sha + ' outcome=STAGE_OK manifest=' + ('a' * 64)); exit 0\n"
)
# A lane-local stage-chain.ps1 copy (hand-copied into a run dir, never committed), in its two shapes.
_COPY_BODY = (
    "param([string]$Sha, [string]$DeadlineUtc, [string]$StageOne)\n"
    "{set}"
    "foreach ($p in 'dll', 'stage') {\n"
    "  & $env:STUB_PWSH -NoProfile -NonInteractive -File $StageOne -Sha $Sha -Phase $p\n"
    "  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "}\n"
    "exit 0\n"
)
COPY_THAT_SETS = _COPY_BODY.replace("{set}", "$env:MLV_HEAVY_SLOT_DEADLINE_UTC = $DeadlineUtc\n")
COPY_THAT_SETS_NOTHING = _COPY_BODY.replace("{set}", "")


def _future(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_trip(second_precision_utc: str) -> str:
    """The chain publishes DateTime.ToString('o'): a UTC 'yyyy-MM-ddTHH:mm:ssZ' becomes '...ss.0000000Z'."""
    return second_precision_utc[:-1] + ".0000000Z"


class _ChainBox:
    """A temp run dir with the stubs, plus the helpers to run a chain over it and read what each hop recorded."""

    def __init__(self, test: unittest.TestCase, assembler_text: str = STUB_ASSEMBLER, assembler_path: "Path | None" = None):
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.run = self.dir / "run"
        self.run.mkdir()
        self.work = self.dir / "work"
        self.work.mkdir()
        (self.dir / "guard.ps1").write_text(STUB_GUARD, encoding="ascii")
        self.assembler = assembler_path or (self.dir / "assemble.ps1")
        if assembler_path is None:
            self.assembler.write_text(assembler_text, encoding="ascii")
        self.stage_one = self.run / "stage-one.ps1"
        self.stage_one.write_text(STAGE_ONE, encoding="ascii")
        self.record = self.dir / "record.txt"
        self.heavy_log = self.dir / "heavy-stub.log"

    def env(self) -> dict:
        env = dict(os.environ)
        env.pop(ENV_NAME, None)  # a driver's deadline never leaks into a test from the machine that runs it
        for name in [n for n in env if n.upper().startswith("STUB_")]:
            del env[name]
        for name in ("HEAVY_SLOT_HELD", "HEAVY_SLOT_HELD_START", "HEAVY_SLOT_HELD_NAME", "MLV_LANE_RUN_DIR", "MLV_BUILD_GUARD_EXEMPT"):
            env.pop(name, None)
        env.update({
            "STUB_PWSH": PWSH, "STUB_GUARD": str(self.dir / "guard.ps1"), "STUB_ASSEMBLER": str(self.assembler),
            "STUB_RECORD": str(self.record), "STUB_WORK": str(self.work),
            # Anything the real assembler might reach must hit the stub, never the machine slot; its log proves nothing was reached.
            "MLV_HEAVY_SLOT_CMD": str(HEAVY_STUB), "STUB_HEAVY_SLOT_LOG": str(self.heavy_log),
        })
        return env

    def run_tracked_chain(self, *chain_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(CHAIN), "-Sha", SHA, "-StageOne", str(self.stage_one),
             "-RetrySec", "0", "-MaxAttempts", "1", *chain_args],
            capture_output=True, text=True, timeout=240, env=self.env())

    def run_copy(self, copy_text: str, deadline: str) -> subprocess.CompletedProcess:
        copy = self.run / "stage-chain.ps1"
        copy.write_text(copy_text, encoding="ascii")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(copy), "-Sha", SHA, "-DeadlineUtc", deadline, "-StageOne", str(self.stage_one)],
            capture_output=True, text=True, timeout=240, env=self.env())

    def records(self) -> "list[str]":
        return self.record.read_text(encoding="utf-8").splitlines() if self.record.exists() else []

    def seen(self, who: str) -> "list[str]":
        """The deadline text each hop named `who` received (the text between [ ])."""
        return [m.group(1) for line in self.records() if line.startswith("who=" + who + " ") for m in [re.search(r"hs=\[(.*)\]$", line)] if m]

    def status(self) -> "list[str]":
        path = self.run / f"stage-status-{SHA[:8]}.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@windows_chain_only
class StageChainPropagationTests(unittest.TestCase):
    """Dot 791-I1 item 1: the deadline is observed arriving at every hop, not read out of the source text."""

    def _assert_every_hop_saw(self, box: _ChainBox, expected: str) -> None:
        self.assertEqual(box.seen("stage-one"), [expected, expected], "stage-one, phase dll and phase stage: " + repr(box.records()))
        self.assertEqual(box.seen("assembler"), [expected], repr(box.records()))
        self.assertEqual(box.seen("guard"), [expected], repr(box.records()))

    def test_the_tracked_chain_hands_the_guard_the_deadline_unchanged(self) -> None:
        deadline = _future(240)
        box = _ChainBox(self)
        done = box.run_tracked_chain("-DeadlineUtc", deadline)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr + "\n".join(box.status()))
        # Unchanged = the same instant, in the one format the chain publishes (round-trip UTC); stage-one declares no -DeadlineUtc, so this is
        # inheritance through two process hops (stage-one -> assembler -> guard), not a parameter.
        self._assert_every_hop_saw(box, _round_trip(deadline))
        self.assertTrue(box.status() and box.status()[-1].endswith("STAGE_CHAIN_DONE"), box.status())

    def test_a_run_dir_copy_that_sets_it_hands_the_guard_the_deadline_unchanged(self) -> None:
        deadline = "2099-01-01T00:00:00.0000000Z"
        box = _ChainBox(self)
        done = box.run_copy(COPY_THAT_SETS, deadline)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self._assert_every_hop_saw(box, deadline)

    def test_a_run_dir_copy_that_sets_nothing_reaches_the_guard_with_no_deadline_the_incident(self) -> None:
        # CONTROL (the incident's shape, 2026-11-08): nothing sets the variable, so even a stub assembler passes the guard nothing.
        # This is exactly the arrival the real assembler's gate (next test) refuses.
        box = _ChainBox(self)
        done = box.run_copy(COPY_THAT_SETS_NOTHING, "2099-01-01T00:00:00.0000000Z")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self._assert_every_hop_saw(box, "")

    def test_a_run_dir_copy_that_sets_nothing_ends_in_the_assemblers_typed_refusal_before_anything_is_taken(self) -> None:
        # RED on fork/master f26387ad (the assembler went on to its DLL-pair step, exit 3). The REAL assembler stands where the stub did.
        box = _ChainBox(self, assembler_path=ASSEMBLER)
        done = box.run_copy(COPY_THAT_SETS_NOTHING, "2099-01-01T00:00:00.0000000Z")
        out = done.stdout + done.stderr
        self.assertNotEqual(done.returncode, 0, out)
        self.assertIn("STAGE_NO_HEAVY_SLOT_DEADLINE", out)
        self.assertIn("RESULT=ASSEMBLE_FAILED STEP=heavySlotDeadline EXIT=27", out)
        self.assertIn("who=assembler-exit code=27", box.records(), "the assembler ended with exit 27 (typed), not a build failure or a guard refusal")
        self.assertEqual(box.seen("guard"), [], "the guard (and so any heavy-slot ticket) was never reached")
        self.assertFalse(box.heavy_log.exists(), "the heavy-slot stub was never called")
        self.assertFalse((box.work / "out").exists(), "nothing was created in -OutDir")
        self.assertIn("outcome=ASSEMBLE_FAILED exit=27", out)

    def test_the_tracked_chain_with_the_real_assembler_is_not_refused_by_the_gate(self) -> None:
        # The other side of the same fixture: with the tracked chain's deadline the real assembler's gate lets the run through to its next step.
        box = _ChainBox(self, assembler_path=ASSEMBLER)
        done = box.run_tracked_chain("-DeadlineUtc", _future(240))
        out = done.stdout + done.stderr + "\n".join(box.status()) + "\n" + "\n".join(
            p.read_text(encoding="utf-8", errors="replace") for p in sorted(box.run.glob("stage-*.out.txt")))
        self.assertNotIn("STAGE_NO_HEAVY_SLOT_DEADLINE", out)
        self.assertNotIn("who=assembler-exit code=27", box.records())
        # Dot 791-I2: the negatives above would also hold for a syntax error, a launch failure or any early crash. The POSITIVE evidence that the
        # real assembler ran PAST its gate: it reached its next step, the source-commit check (-RepoRoot is a temp dir, not a git repo, so
        # `git cat-file -e` fails), and ended with that step's own typed refusal and exit code, recorded by stage-one.
        self.assertIn("RESULT=ASSEMBLE_FAILED STEP=sourceCommit EXIT=2", out)
        self.assertIn("who=assembler-exit code=2", box.records())
        self.assertIn("outcome=ASSEMBLE_FAILED exit=2", out)


@windows_chain_only
class ChainLayerBoundaryTests(unittest.TestCase):
    """Dot 791-I1 item 2, the tracked chain's layer (the assembler's rows are in test_stage_assembler_requires_heavy_slot_deadline)."""

    def _refused(self, deadline_args: "list[str]", reason: str) -> None:
        box = _ChainBox(self)
        done = box.run_tracked_chain(*deadline_args)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn(f"STAGE_CHAIN_REFUSED reason={reason}", done.stdout)
        self.assertEqual(box.records(), [], "refused before any stage-one child started")
        self.assertEqual(box.status(), [], "refused before the status file was opened")

    def test_boundary_absent_is_refused_by_the_chain_with_invalid_deadline(self) -> None:
        self._refused([], "invalid-deadline")

    def test_boundary_blank_and_whitespace_are_refused_by_the_chain_with_invalid_deadline(self) -> None:
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                self._refused(["-DeadlineUtc", blank], "invalid-deadline")

    def test_boundary_malformed_is_refused_by_the_chain_with_invalid_deadline(self) -> None:
        for bad in ("tomorrow", "2099-13-45T00:00:00Z", "2099-01-01", "2099-01-01 00:00:00Z", "2099-01-01T00:00:00 UTC", "00:00:00.Z"):
            with self.subTest(deadline=bad):
                self._refused(["-DeadlineUtc", bad], "invalid-deadline")

    def test_boundary_non_utc_offset_is_accepted_and_the_child_gets_the_same_instant_in_utc(self) -> None:
        box = _ChainBox(self)
        done = box.run_tracked_chain("-DeadlineUtc", "2099-01-01T05:00:00+05:00")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(box.seen("guard"), ["2099-01-01T00:00:00.0000000Z"], box.records())
        box = _ChainBox(self)
        box.run_tracked_chain("-DeadlineUtc", "2098-12-31T19:00:00-05:00")
        self.assertEqual(box.seen("guard"), ["2099-01-01T00:00:00.0000000Z"], box.records())

    def test_boundary_already_expired_starts_no_attempt_and_is_a_capacity_exit(self) -> None:
        box = _ChainBox(self)
        done = box.run_tracked_chain("-DeadlineUtc", "2000-01-01T00:00:00Z")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(box.records(), [], "no stage-one child was launched")
        self.assertTrue(any("STAGE_FAILED deadline-passed phase=dll" in line for line in box.status()), box.status())

    def test_boundary_expiring_inside_any_minimum_build_window_is_not_refused_unproved_contract_limit(self) -> None:
        # UNPROVED CONTRACT LIMIT, pinned as it is: no layer (assembler gate, chain, guard) enforces a minimum build window. A deadline two
        # minutes out starts the chain; the guard then bounds the first ticket's --wait by what is left. If a layer ever starts refusing
        # such a deadline, this test goes red and the limit is retired deliberately.
        deadline = _future(2)
        box = _ChainBox(self)
        done = box.run_tracked_chain("-DeadlineUtc", deadline)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(box.seen("guard"), [_round_trip(deadline)], box.records())

    def test_boundary_valid_is_passed_through_as_the_round_trip_instant(self) -> None:
        deadline = _future(180)
        box = _ChainBox(self)
        done = box.run_tracked_chain("-DeadlineUtc", deadline)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(box.seen("guard"), [_round_trip(deadline)], box.records())
        self.assertIn(f"deadline={_round_trip(deadline)} ", box.status()[0])


# --- the AST scan of every tracked caller (Dot 791-I1 item 3) ---------------------------------------------------------------------------------

# Reads each file in -ListFile with [Parser]::ParseFile and prints one JSON object per line: either a parse error, or a CALL SITE of the assembler.
# A reference is a string constant (never a comment) that IS a path ending in the assembler's file name (a usage message or a template that only mentions it is not one). A site is a command that takes the assembler as an element: the
# string itself, a variable assigned from an expression holding it, or a parenthesised/sub expression holding it -- unless the command only builds or
# tests a path. A site is COVERED when (a) the same command carries -ChildEnv @{ MLV_HEAVY_SLOT_DEADLINE_UTC = <non-blank> }, or (b) an earlier
# statement in the same enclosing script block sets $env:MLV_HEAVY_SLOT_DEADLINE_UTC / ...Environment['MLV_HEAVY_SLOT_DEADLINE_UTC'] to a non-blank
# value. Anything it cannot read (a -ChildEnv that is not a literal hashtable, say) is NOT covered: it fails closed.
_SCAN_SCRIPT = r"""
param([Parameter(Mandatory = $true)][string]$ListFile, [string]$Needle, [string]$EnvName)
$ErrorActionPreference = 'Stop'
$A = 'System.Management.Automation.Language'
$PathOnly = @('join-path', 'test-path', 'resolve-path', 'split-path', 'get-item', 'get-content', 'get-childitem', 'get-filehash', 'convert-path')
$isRef = { param($n) (($n -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or ($n -is [System.Management.Automation.Language.ExpandableStringExpressionAst])) -and ($n.Value -match ('(^|[\\/])' + [regex]::Escape($script:Needle) + '\.ps1$')) }
$blank = { param([string]$t) $x = $t -replace '`u\{[0-9a-fA-F]+\}', '' -replace '`[a-zA-Z0-9]', '' -replace '\[[A-Za-z.]+\]', '' -replace '[\s''"()@{}$]', ''; ($x -eq '') -or ($x -eq 'null') }
foreach ($file in [IO.File]::ReadAllLines($ListFile)) {
    if (-not $file) { continue }
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$null, [ref]$errs)
    if ($errs -and $errs.Count -gt 0) { [Console]::Out.WriteLine((@{ file = $file; kind = 'parse-error'; line = $errs[0].Extent.StartLineNumber; shape = 'parse-error'; covered = $false } | ConvertTo-Json -Compress)); continue }
    if (-not $ast.Find($isRef, $true)) { continue }
    $names = @{}
    foreach ($as in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($as.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and $as.Right.Find($isRef, $true)) { $names[$as.Left.VariablePath.UserPath.ToLowerInvariant()] = $true }
    }
    $denotes = {
        param($e)
        if (& $isRef $e) { return $true }
        if ($e -is [System.Management.Automation.Language.VariableExpressionAst]) { return $names.ContainsKey($e.VariablePath.UserPath.ToLowerInvariant()) }
        if ($e -is [System.Management.Automation.Language.ParenExpressionAst] -or $e -is [System.Management.Automation.Language.SubExpressionAst]) { return [bool]$e.Find({ param($n) (& $isRef $n) -or ($n -is [System.Management.Automation.Language.VariableExpressionAst] -and $names.ContainsKey($n.VariablePath.UserPath.ToLowerInvariant())) }, $true) }
        return $false
    }
    foreach ($cmd in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $first = $cmd.GetCommandName()
        if ($first -and $PathOnly -contains $first.ToLowerInvariant()) { continue }
        $hit = $false
        foreach ($el in $cmd.CommandElements) { if (& $denotes $el) { $hit = $true; break } }
        if (-not $hit) { continue }
        $shape = 'none'; $covered = $false
        $els = @($cmd.CommandElements)
        for ($i = 0; $i -lt $els.Count; $i++) {
            $el = $els[$i]
            if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'ChildEnv') {
                $arg = $el.Argument
                if ($null -eq $arg -and $i + 1 -lt $els.Count) { $arg = $els[$i + 1] }
                if ($arg -is [System.Management.Automation.Language.HashtableAst]) {
                    foreach ($kv in $arg.KeyValuePairs) {
                        $key = $kv.Item1
                        if ($key -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $key.Value -eq $EnvName -and -not (& $blank $kv.Item2.Extent.Text)) { $covered = $true; $shape = 'ChildEnv' }
                    }
                    if (-not $covered) { $shape = 'ChildEnv-without-a-deadline' }
                } else { $shape = 'ChildEnv-not-a-literal-hashtable' }
            }
        }
        if (-not $covered) {
            $scope = $cmd.Parent
            while ($null -ne $scope -and $scope -isnot [System.Management.Automation.Language.ScriptBlockAst]) { $scope = $scope.Parent }
            if ($null -ne $scope) {
                foreach ($as in $scope.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Extent.StartOffset -lt $cmd.Extent.StartOffset }, $true)) {
                    $l = $as.Left
                    $isEnv = ($l -is [System.Management.Automation.Language.VariableExpressionAst] -and $l.VariablePath.UserPath -ieq ('env:' + $EnvName)) -or
                        ($l -is [System.Management.Automation.Language.IndexExpressionAst] -and $l.Index -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $l.Index.Value -eq $EnvName -and $l.Target.Extent.Text -match '\.Environment$')
                    if ($isEnv -and -not (& $blank $as.Right.Extent.Text)) { $covered = $true; $shape = 'scoped-env-assignment' }
                }
            }
        }
        [Console]::Out.WriteLine((@{ file = $file; kind = 'site'; line = $cmd.Extent.StartLineNumber; shape = $shape; covered = $covered; command = $cmd.Extent.Text.Substring(0, [Math]::Min(120, $cmd.Extent.Text.Length)) } | ConvertTo-Json -Compress))
    }
}
"""


def _scan(files: "list[Path]") -> "list[dict]":
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "scan.ps1"
        script.write_text(_SCAN_SCRIPT, encoding="ascii")
        listing = Path(tmp) / "files.txt"
        listing.write_text("\n".join(str(f) for f in files) + "\n", encoding="utf-8")
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), "-ListFile", str(listing),
                               "-Needle", NEEDLE, "-EnvName", ENV_NAME], capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise AssertionError("the AST scan itself failed: " + done.stdout + done.stderr)
    return [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]


def _tracked_files_naming_the_assembler() -> "list[str]":
    done = subprocess.run([GIT, "-C", str(ROOT), "grep", "-l", "-i", "-I", NEEDLE], capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


@unittest.skipUnless(PWSH and GIT, "needs pwsh and git")
class TrackedAssemblerCallerAstTests(unittest.TestCase):
    """Dot 791-I1 item 3. UNPROVED CONTRACT LIMITS (named in the PR body): this scan reads PowerShell syntax trees of tracked .ps1/.psm1 files that
    NAME the assembler in a string. It does not cover a name built at run time (concatenation, -f, an env var, a splatted table), Invoke-Expression,
    a remote shell (ssh) command line, an alias, a caller in another language (only an inventory by name, below), or an UNTRACKED run-dir copy
    (the assembler's own exit-27 gate is the backstop there). A tracked chain that reaches the assembler through a stage-one script it does not name
    (Invoke-VenueStageChain.ps1) is proved by StageChainPropagationTests, not by this scan. Two more, from the adversary pass on 44f72c7a (minor,
    advisory): a scoped $env: assignment covers a call by POSITION in the same script block, not by reachability (an `if ($false)` guard, a later
    clear, or an uncalled function would still count); and a string that is itself a path to the assembler, even in a log call, is counted as a
    site (conservative: the scan goes red on a harmless line rather than miss a call)."""

    def _tracked_powershell_callers(self) -> "list[Path]":
        return [ROOT / rel for rel in _tracked_files_naming_the_assembler()
                if rel.endswith((".ps1", ".psm1")) and Path(rel) != ASSEMBLER.relative_to(ROOT)]

    def test_every_tracked_powershell_caller_is_covered_and_the_nightly_is_found(self) -> None:
        sites = _scan(self._tracked_powershell_callers())
        bad = [s for s in sites if not s["covered"]]
        self.assertEqual(bad, [], "a tracked caller reaches the assembler without handing it a deadline: " + json.dumps(bad, indent=1))
        nightly = [s for s in sites if Path(s["file"]) == NIGHTLY]
        self.assertEqual(len(nightly), 1, json.dumps(sites, indent=1))
        self.assertEqual(nightly[0]["shape"], "ChildEnv")
        self.assertIn("'assemble'", nightly[0]["command"])

    def test_the_only_non_powershell_files_naming_the_assembler_are_docs_and_tests(self) -> None:
        # Inventory by name for the shapes the AST scan cannot read: a new caller in another language (or a workflow) is a RED here, to be reviewed.
        other = [rel for rel in _tracked_files_naming_the_assembler()
                 if not rel.endswith((".ps1", ".psm1")) and not rel.endswith(".md")
                 and not (rel.startswith("tests/") or (rel.startswith("tools/repo_hygiene/test_") and rel.endswith(".py")))]
        self.assertEqual(other, [], "a non-PowerShell file names the assembler; add a proof for that caller shape")

    # -- widening mutations: each starts from the real nightly and must go RED (or, as a control, stay GREEN) ------------------------------

    def _mutate(self, edit) -> "list[dict]":
        text = NIGHTLY.read_text(encoding="utf-8")
        mutated = edit(text)
        self.assertNotEqual(mutated, text, "the mutation changed nothing")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Invoke-VenueStageNightly.ps1"
            path.write_text(mutated, encoding="utf-8")
            return _scan([path])

    def _uncovered(self, sites: "list[dict]") -> "list[dict]":
        return [s for s in sites if not s["covered"]]

    def test_mutation_dropping_the_childenv_goes_red(self) -> None:
        def edit(t: str) -> str:
            return re.sub(r" -ChildEnv @\{ MLV_HEAVY_SLOT_DEADLINE_UTC = [^}]*\}", "", t, count=1)
        self.assertEqual([s["shape"] for s in self._uncovered(self._mutate(edit))], ["none"])

    def test_mutation_blanking_the_deadline_value_goes_red(self) -> None:
        sites = self._mutate(lambda t: t.replace("MLV_HEAVY_SLOT_DEADLINE_UTC = $script:Deadline.ToString('o')", "MLV_HEAVY_SLOT_DEADLINE_UTC = ''", 1))
        self.assertEqual([s["shape"] for s in self._uncovered(sites)], ["ChildEnv-without-a-deadline"])

    def test_mutation_whitespace_deadline_value_goes_red_adversary_a1(self) -> None:
        # adversary 44f72c7a-A1: '   ' passed the scan as a deadline, but the assembler refuses it (exit 27).
        for blank in ("'   '", '"  "', "$null", "''", '"`t"', "($null)", "@()", "@( )", "[string]$null", '"`v"', '"`f"', '"`u{0020}"'):
            with self.subTest(value=blank):
                sites = self._mutate(lambda t: t.replace("MLV_HEAVY_SLOT_DEADLINE_UTC = $script:Deadline.ToString('o')", "MLV_HEAVY_SLOT_DEADLINE_UTC = " + blank, 1))
                self.assertEqual([s["shape"] for s in self._uncovered(sites)], ["ChildEnv-without-a-deadline"])

    def test_mutation_misspelling_the_variable_goes_red(self) -> None:
        sites = self._mutate(lambda t: t.replace("-ChildEnv @{ MLV_HEAVY_SLOT_DEADLINE_UTC =", "-ChildEnv @{ MLV_HEAVY_SLOT_DEADLINE_UTX =", 1))
        self.assertEqual([s["shape"] for s in self._uncovered(sites)], ["ChildEnv-without-a-deadline"])

    def test_mutation_passing_the_environment_as_a_variable_fails_closed(self) -> None:
        sites = self._mutate(lambda t: re.sub(r"-ChildEnv @\{ MLV_HEAVY_SLOT_DEADLINE_UTC = [^}]*\}", "-ChildEnv $someEnv", t, count=1))
        self.assertEqual([s["shape"] for s in self._uncovered(sites)], ["ChildEnv-not-a-literal-hashtable"])

    def test_mutation_adding_a_second_direct_call_without_a_deadline_goes_red(self) -> None:
        sites = self._mutate(lambda t: t + "\n& $Assembler -SourceCommit $commit -DllPairDir $dllDir -OutDir $buildDir\n")
        self.assertEqual(len(sites), 2)
        self.assertEqual([s["covered"] for s in sites], [True, False])

    def test_mutation_adding_a_path_expression_call_goes_red(self) -> None:
        sites = self._mutate(lambda t: t + "\n& (Join-Path $PSScriptRoot '..\\bachelor\\playback-attr-3-cuda-assemble.ps1') -SourceCommit $commit\n")
        self.assertEqual([s["covered"] for s in sites], [True, False])

    def test_control_a_direct_call_after_an_env_assignment_in_the_same_scope_is_covered(self) -> None:
        sites = self._mutate(lambda t: t + "\n$env:MLV_HEAVY_SLOT_DEADLINE_UTC = $script:Deadline.ToString('o')\n& $Assembler -SourceCommit $commit\n")
        self.assertEqual([(s["covered"], s["shape"]) for s in sites], [(True, "ChildEnv"), (True, "scoped-env-assignment")])

    def test_control_an_env_assignment_after_the_call_does_not_cover_it(self) -> None:
        sites = self._mutate(lambda t: t + "\n& $Assembler -SourceCommit $commit\n$env:MLV_HEAVY_SLOT_DEADLINE_UTC = $script:Deadline.ToString('o')\n")
        self.assertEqual([s["covered"] for s in sites], [True, False])

    def test_control_a_comment_or_a_log_line_naming_the_assembler_is_not_a_call(self) -> None:
        # The textual check this replaces counted these. In the syntax tree a comment is not a node and a log line's string is an argument of Write-Host
        # that is not the assembler's own invocation path.
        sites = self._mutate(lambda t: t + "\n# & tools\\profiling\\bachelor\\playback-attr-3-cuda-assemble.ps1 -SourceCommit x   (a comment)\n")
        self.assertEqual([s["covered"] for s in sites], [True])

    def test_mutation_an_upper_case_path_caller_is_found_adversary_a2(self) -> None:
        # Windows resolves the path case-insensitively, so the scan and the inventory (git grep -i) must too.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Invoke-Other.ps1"
            path.write_text('param($c)\n& "$PSScriptRoot\\..\\bachelor\\PLAYBACK-ATTR-3-CUDA-ASSEMBLE.ps1" -SourceCommit $c\n', encoding="ascii")
            sites = _scan([path])
        self.assertEqual([(s["covered"], s["shape"]) for s in sites], [(False, "none")])

    def test_mutation_calls_through_an_assigned_variable_inside_parens_are_found_adversary_a2(self) -> None:
        for call in ("& ($a) -SourceCommit $c", "& $($a) -SourceCommit $c"):
            with self.subTest(call=call):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "Invoke-Other.ps1"
                    path.write_text("param($c)\n$a = 'bachelor/playback-attr-3-cuda-assemble.ps1'\n" + call + "\n", encoding="ascii")
                    sites = _scan([path])
                self.assertEqual([(s["covered"], s["shape"]) for s in sites], [(False, "none")])

    def test_mutation_a_fake_environment_dictionary_does_not_cover_a_call_adversary_a3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Invoke-Other.ps1"
            path.write_text("param($c)\n$fakeEnvironment = @{}\n$fakeEnvironment['MLV_HEAVY_SLOT_DEADLINE_UTC'] = '2099-01-01T00:00:00Z'\n"
                            "& 'bachelor/playback-attr-3-cuda-assemble.ps1' -SourceCommit $c\n", encoding="ascii")
            sites = _scan([path])
        self.assertEqual([(s["covered"], s["shape"]) for s in sites], [(False, "none")])

    def test_mutation_a_second_caller_file_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Invoke-Other.ps1"
            path.write_text("param($c)\n$a = Join-Path $PSScriptRoot 'bachelor\\playback-attr-3-cuda-assemble.ps1'\n& $a -SourceCommit $c\n", encoding="ascii")
            sites = _scan([path])
        self.assertEqual([(s["covered"], s["shape"]) for s in sites], [(False, "none")])


if __name__ == "__main__":
    unittest.main()
