"""PLAYBACK-CUDA-NATIVE-PACE-1 -- the playback oracle judges the OBSERVED timeline rate, not only the DECLARED pace.

On Ultra-Magnus (RTX 4090) the CUDA texture route showed a 23.976 fps clip at 31.6 / 30.4 timeline fps (600 source frames in
18.98 s / 19.71 s of measured Play), yet every oracle copy said "paced at native fps" and valid: each read ``pace_fps`` (the pace
the engine was TOLD), which still said 23.976. The observed rate is source_advanced / elapsed seconds of the measured Play; more
than 2 % over native is INVALID with the typed reason PLAYBACK_FASTER_THAN_NATIVE. Slower than native (drop / hold, a slow venue,
the CPU leg) stays exactly as before. Pinned here in all three copies, each mutation-tested:

  * the local gate (gui-smoke-length-gate.ps1, Get-GuiSmokeSourceFramesVerdict via Get-GuiSmokeEvidencePlayVerdict) -- it also
    judges the profile receipt, which carries no Play wall time, so it judges the rate wherever elapsed_ms is present;
  * the venue job (AttrCudaArtifacts.psm1, Get-AttrCudaSourceFramesVerdict) -- reads the smoke summary, elapsed_ms REQUIRED;
  * the dual-venue receipt (DualVenueRunner.psm1, Get-DvPlaybackProblems) -- elapsed_ms REQUIRED.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_clip_length_gate import GATE, PROFILING, RUN_NONCE, _pwsh, _q, requires_pwsh
from tools.repo_hygiene.test_playback_launcher_receipt_oracle import _good_line, _oracle
from tools.repo_hygiene.test_dual_venue_evidence import ModuleMutationMixin, good_block, ps_problems, requires_windows_pwsh

MODULE = PROFILING / "bachelor" / "AttrCudaArtifacts.psm1"
JOB = PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1"
NATIVE = 23.976
TOKEN = "PLAYBACK_FASTER_THAN_NATIVE"

# The two Ultra-Magnus CUDA legs (receipts aaa3458d / f68d8d8a, from their run logs) and Bachelor's slower CUDA leg.
UM_CINEMATIC = dict(source_advanced=600, required_source_frames=600, elapsed_ms=18978.75)   # 31.61 fps
UM_CLASSIC = dict(source_advanced=600, required_source_frames=600, elapsed_ms=19705.321)    # 30.45 fps
BACHELOR = dict(source_advanced=600, required_source_frames=600, elapsed_ms=27397.0)        # 21.90 fps
AT_NATIVE = dict(source_advanced=600, required_source_frames=600, elapsed_ms=25026.0)       # 23.98 fps (the engine ceiling)


def _elapsed_for(ratio: float, frames: int = 600) -> float:
    """The elapsed_ms at which `frames` source frames are `ratio` x native."""
    return round(frames * 1000.0 / (NATIVE * ratio), 3)


def _without(line: str, key: str) -> str:
    return re.sub(rf"\b{key}=\S+", "", line)


def _module_verdict(line: str, module: Path = MODULE) -> dict:
    proc = _pwsh(f"Import-Module {_q(module)} -Force -DisableNameChecking; "
                 f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
                 "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures) } | ConvertTo-Json -Compress")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    verdict["fasterThanNative"] = any(str(f).startswith(TOKEN + ":") for f in verdict["failures"])
    return verdict


def _receipt_block(**fields: object) -> dict:
    advanced = fields.pop("source_advanced", 600)
    return good_block(sourceAdvanced=advanced, jobSourceAdvanced=advanced, requiredSourceFrames=600, jobRequiredSourceFrames=600,
                      **{"elapsedMs": fields.pop("elapsed_ms")}, **fields)


def _mutated(path: Path, old: str, new: str, tmp: str) -> Path:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation anchor must occur exactly once in {path.name}: {old!r}"
    out = Path(tmp) / path.name
    out.write_text(text.replace(old, new), encoding="utf-8")
    return out


@requires_pwsh
class GateObservedRateTests(unittest.TestCase):
    """The local gate (gui-smoke-length-gate.ps1), executed through the verdict the runner calls."""

    def test_the_ultra_magnus_cuda_legs_are_invalid_faster_than_native(self) -> None:
        for name, leg in (("cinematic", UM_CINEMATIC), ("classic", UM_CLASSIC)):
            with self.subTest(name):
                verdict = _oracle(_good_line(**leg))
                self.assertTrue(verdict["invalid"], verdict)
                self.assertIn(TOKEN, json.dumps(verdict["failures"]))

    def test_slower_than_native_and_native_stay_valid(self) -> None:
        for name, leg in (("bachelor 21.9 fps", BACHELOR), ("at native", AT_NATIVE)):
            with self.subTest(name):
                verdict = _oracle(_good_line(**leg))
                self.assertFalse(verdict["invalid"], verdict)

    def test_the_tolerance_is_two_percent(self) -> None:
        self.assertFalse(_oracle(_good_line(source_advanced=600, required_source_frames=600, elapsed_ms=_elapsed_for(1.019)))["invalid"])
        late = _oracle(_good_line(source_advanced=600, required_source_frames=600, elapsed_ms=_elapsed_for(1.021)))
        self.assertTrue(late["invalid"], late)
        self.assertIn(TOKEN, json.dumps(late["failures"]))

    def test_a_receipt_without_a_play_wall_time_is_not_judged_on_rate(self) -> None:
        # the profile receipt carries no elapsed_ms; the rate rule must not invent one
        self.assertFalse(_oracle(_without(_good_line(**UM_CINEMATIC), "elapsed_ms"))["invalid"])

    def test_mutation_without_the_rate_check_the_ultra_magnus_leg_is_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pace-gate-mut-") as tmp:
            mutated = _mutated(GATE, "if ($ObservedFps -le $native * (1.0 + $script:GuiSmokeFasterThanNativeTolerance)) { return $null }",
                               "return $null", tmp)
            self.assertFalse(_oracle(_good_line(**UM_CINEMATIC), gate=mutated)["invalid"])


@requires_pwsh
class VenueJobObservedRateTests(unittest.TestCase):
    """The venue job's copy (AttrCudaArtifacts.psm1), which reads the smoke summary line itself."""

    def test_the_ultra_magnus_cuda_legs_are_invalid_faster_than_native(self) -> None:
        for leg in (UM_CINEMATIC, UM_CLASSIC):
            verdict = _module_verdict(_good_line(**leg))
            self.assertTrue(verdict["invalid"], verdict)
            self.assertTrue(verdict["fasterThanNative"], verdict)
            self.assertIn(TOKEN, json.dumps(verdict["failures"]))
        self.assertIn("31.614 fps observed", json.dumps(_module_verdict(_good_line(**UM_CINEMATIC))["failures"]))

    def test_slower_than_native_and_native_stay_valid(self) -> None:
        for leg in (BACHELOR, AT_NATIVE):
            verdict = _module_verdict(_good_line(**leg))
            self.assertFalse(verdict["invalid"], verdict)
            self.assertFalse(verdict["fasterThanNative"], verdict)

    def test_an_absent_or_zero_wall_time_is_invalid_never_unchecked(self) -> None:
        absent = _module_verdict(_without(_good_line(**UM_CINEMATIC), "elapsed_ms"))
        self.assertTrue(absent["invalid"], absent)
        self.assertIn("RECEIPT_FIELD_ABSENT", json.dumps(absent["failures"]))
        zero = _module_verdict(_good_line(**{**UM_CINEMATIC, "elapsed_ms": 0}))
        self.assertTrue(zero["invalid"], zero)
        self.assertIn("elapsed_ms=0", json.dumps(zero["failures"]))

    def test_the_tolerance_is_two_percent(self) -> None:
        self.assertFalse(_module_verdict(_good_line(source_advanced=600, required_source_frames=600, elapsed_ms=_elapsed_for(1.019)))["invalid"])
        self.assertTrue(_module_verdict(_good_line(source_advanced=600, required_source_frames=600, elapsed_ms=_elapsed_for(1.021)))["fasterThanNative"])

    def test_mutation_without_the_rate_check_the_ultra_magnus_leg_is_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pace-module-mut-") as tmp:
            mutated = _mutated(MODULE, "$observedTimelineFps -gt $nativeForRate * 1.02", "$false", tmp)
            self.assertFalse(_module_verdict(_good_line(**UM_CINEMATIC), module=mutated)["invalid"])

    def test_the_job_names_the_typed_refusal(self) -> None:
        text = JOB.read_text(encoding="utf-8")
        self.assertIn("-like 'PLAYBACK_FASTER_THAN_NATIVE:*'", text)
        self.assertIn("$sourceFramesRefusal['smokeRefusalReason'] = 'PLAYBACK_FASTER_THAN_NATIVE'", text)
        # the override sits before the refusal is written
        self.assertLess(text.index("$sourceFramesRefusal['smokeRefusalReason'] = 'PLAYBACK_FASTER_THAN_NATIVE'"),
                        text.index("Save-Json $sourceFramesRefusal (Join-Path $Pub 'summary.json')"))


@requires_windows_pwsh
class ReceiptObservedRateTests(ModuleMutationMixin, unittest.TestCase):
    """The dual-venue receipt (DualVenueRunner.psm1), judged from the receipt's own fields."""

    def test_the_ultra_magnus_cuda_legs_are_invalid_faster_than_native(self) -> None:
        for leg in (UM_CINEMATIC, UM_CLASSIC):
            problems = ps_problems(_receipt_block(**leg))
            self.assertTrue(any(p.startswith(TOKEN) for p in problems), problems)

    def test_slower_than_native_and_native_stay_valid(self) -> None:
        for leg in (BACHELOR, AT_NATIVE):
            self.assertEqual(ps_problems(_receipt_block(**leg)), [])

    def test_an_absent_or_zero_wall_time_is_invalid_never_unchecked(self) -> None:
        self.assertIn("RECEIPT_FIELD_ABSENT: elapsed_ms", ps_problems(_receipt_block(elapsed_ms=None)))
        self.assertTrue(any("elapsed_ms=0" in p for p in ps_problems(_receipt_block(elapsed_ms=0.0))))

    def test_the_tolerance_is_two_percent(self) -> None:
        self.assertEqual(ps_problems(_receipt_block(elapsed_ms=_elapsed_for(1.019))), [])
        self.assertTrue(any(p.startswith(TOKEN) for p in ps_problems(_receipt_block(elapsed_ms=_elapsed_for(1.021)))))

    def test_mutation_without_the_rate_check_the_ultra_magnus_leg_is_valid(self) -> None:
        mutated = self.mutated_module([("if ($observed -gt $native * (1.0 + $script:FasterThanNativeTolerance)) {", "if ($false) {")])
        self.assertEqual(ps_problems(_receipt_block(**UM_CINEMATIC), module=mutated), [])
        self.assertNotEqual(ps_problems(_receipt_block(**UM_CINEMATIC)), [])


if __name__ == "__main__":
    unittest.main()
