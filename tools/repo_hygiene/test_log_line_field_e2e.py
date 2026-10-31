"""PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A1/A2 on #726): the end-to-end hostile-environment regression.

A launcher environment value holding CR or LF (MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS="150\\rprep_conflated_move_active=1",
MLVAPP_FORCE_SINGLETHREAD="0\\rworker_threads=8") passed the app's env-for-log sanitizer, CrashForensics turned it into a separator, and
DualVenueRunner read the forged token: an ON leg counted as engaged, and a 12-worker run counted as honoured at cap 8. The run logs under
tests/fixtures/log_line_field/ were written by the app's real sanitizer and CrashForensics fold (green-*: the r2 head; red-*: the r1
head's transformation, i.e. the log of a binary built before r2; see that folder's README.md). test_log_line_field.cpp pins the green
files to the code byte for byte. Here DualVenueRunner's own evidence functions read both sets: nothing may count."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import requires_windows_pwsh, run_pwsh_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "log_line_field"
VARIANTS_PREP = ("cr-prep", "lf-prep", "nbsp-prep")
VARIANTS_WORKER = ("cr-worker", "lf-worker", "ls-worker")
ON = {"generatorArgs": {"prepConflatedMove": True}}
OFF = {"generatorArgs": {}}
CAP8 = {"generatorArgs": {"playbackMaxThreads": 8}}
TERMINAL = {"result": "GPU_RECON_FRAMES_ZERO"}


def fixture(prefix: str, variant: str) -> str:
    return (FIXTURES / f"{prefix}-{variant}.log").read_bytes().decode("utf-8")


@requires_windows_pwsh
class HostileEnvironmentRunLogTests(unittest.TestCase):
    def prep(self, spec: dict, log: str) -> dict:
        proc, out = run_pwsh_json("$s = $env:DVE_SPEC | ConvertFrom-Json\n(Get-DvPrepConflatedMoveEvidence -Spec $s -LogText $env:DVE_LOG) | ConvertTo-Json -Compress",
                                  DVE_SPEC=json.dumps(spec), DVE_LOG=log)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        return out

    def workers(self, spec: dict, log: str, summary: dict) -> dict:
        proc, out = run_pwsh_json("$s = $env:DVE_SPEC | ConvertFrom-Json\n$m = $env:DVE_SUMMARY | ConvertFrom-Json\n"
                                  "(Get-DvWorkerThreadsEvidence -Spec $s -LogText $env:DVE_LOG -Summary $m) | ConvertTo-Json -Compress",
                                  DVE_SPEC=json.dumps(spec), DVE_LOG=log, DVE_SUMMARY=json.dumps(summary))
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        return out

    def test_the_fixtures_carry_the_hostile_value_the_way_each_app_wrote_it(self) -> None:
        # red: the forged token stands on the start line (CR -> space, LF -> " | ") or behind a raw Unicode space; green: it is folded into its env field.
        self.assertIn(" env_playback_scope_interval_ms=150 prep_conflated_move_active=1 ", fixture("red", "cr-prep"))
        self.assertIn(" env_playback_scope_interval_ms=150 | prep_conflated_move_active=1 ", fixture("red", "lf-prep"))
        self.assertIn(" env_force_singlethread=0 | worker_threads=8 ", fixture("red", "lf-worker"))
        self.assertIn("env_force_singlethread=0 worker_threads=8", fixture("red", "ls-worker"))
        for variant in VARIANTS_PREP + VARIANTS_WORKER:
            with self.subTest(variant=variant):
                self.assertNotIn("playback_smoke.prep_route", fixture("red", variant))
                green = fixture("green", variant)
                self.assertTrue(all(c == "\n" or " " <= c <= "~" for c in green), variant)
                self.assertEqual(green.count(" worker_threads="), 1)
                self.assertEqual(green.count(" prep_conflated_move_active="), 1)
                self.assertIn("] playback_smoke.prep_route session=3 prep_conflated_move_active=0\n", green)

    def test_a_forged_engagement_field_never_counts_as_on(self) -> None:
        for prefix in ("red", "green"):
            for variant in VARIANTS_PREP:
                with self.subTest(prefix=prefix, variant=variant):
                    v = self.prep(ON, fixture(prefix, variant))
                    self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
                    self.assertIn("ROUTE_NOT_ENGAGED", v["reason"])

    def test_the_off_arm_reads_the_apps_own_record_not_the_forgery(self) -> None:
        for prefix in ("red", "green"):
            for variant in VARIANTS_PREP:
                with self.subTest(prefix=prefix, variant=variant):
                    v = self.prep(OFF, fixture(prefix, variant))
                    self.assertEqual((v["verdict"], v["blocks"]), ("NOT_REQUESTED", False), v)

    def test_a_forged_worker_count_never_counts_as_honoured(self) -> None:
        for prefix in ("red", "green"):
            for variant in VARIANTS_WORKER:
                with self.subTest(prefix=prefix, variant=variant):
                    v = self.workers(CAP8, fixture(prefix, variant), TERMINAL)
                    self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
        for variant in ("cr-worker", "lf-worker"):
            with self.subTest(duplicate=variant):
                self.assertIn("DUPLICATE_START_FIELD", self.workers(CAP8, fixture("red", variant), TERMINAL)["reason"])
        self.assertEqual(self.workers(CAP8, fixture("green", "cr-worker"), TERMINAL)["observed"], 12)


if __name__ == "__main__":
    unittest.main()
