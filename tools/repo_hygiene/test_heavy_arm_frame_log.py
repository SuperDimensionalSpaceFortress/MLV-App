"""Tests for PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 round 2 (sol r1 blocker): a HEAVY telemetry arm keeps its per-frame rows.

CLASS: the generated job hands the smoke runner an -ExtraEnvironment list, and the runner builds the app's environment from its OWN process environment plus that
list. The app (platform/qt/MainWindow.cpp environmentFlagEnabled) suppresses the playback_smoke.frame rows for ANY value of
MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG that is not empty / 0 / false / off / no, and Get-FrameRows then throws 'only 0 high-resolution frame rows'
before the leg publishes. LIGHT sets the flag to 1 on purpose; HEAVY used to say nothing, so an inherited 1 (a shell, a Scheduled Task env block) turned a
HEAVY leg into a LIGHT one. The job now names the flag in the HEAVY arm too, at its off value.

Source: sol r1 on PR 711. Keys and shapes only: the generator runs against header-only stand-in clips, no footage is read, and no app is launched. The
environment the app would see is rebuilt from the job's own text (the real `$telemetryArmEnvs` .. `$envs` slice, executed by pwsh) and the app's own flag rule.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_dual_venue_evidence import (
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    lf,
    requires_windows_pwsh,
    run_pwsh,
)

FLAG = "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG"
LEGS = ROOT / "tools" / "profiling" / "dual-venue" / "legs"
NEW_SPEC = LEGS / "m16-1243-pace-cinematic-cpu-receipt-fullscreen-s4-heavy.json"
# platform/qt/MainWindow.cpp environmentFlagEnabled(): the values that leave the flag OFF (frame rows kept).
APP_OFF_VALUES = ("", "0", "false", "off", "no")
SLICE_START = "$telemetryArmEnvs = if ($TelemetryArm -eq 'LIGHT') {"
SLICE_END = '$envList = "\'" + ($envs -join "\',\'") + "\'"'


def app_frame_rows_suppressed(value: str | None) -> bool:
    """The app's rule for the flag, applied to the value the child process ends up with (None = not set at all)."""
    return value is not None and value.strip().lower() not in APP_OFF_VALUES


def child_flag_value(envs: list[str], inherited: str | None) -> str | None:
    """What the app sees: the runner's Add-EnvironmentPairs writes each KEY=VALUE onto the inherited environment, the last entry for a key wins."""
    value = inherited
    for entry in envs:
        key, _, rest = entry.partition("=")
        if key.strip().upper() == FLAG:
            value = rest
    return value


def spec_generator_args(path: Path) -> list[str]:
    """The generator arguments a committed spec asks for, for the keys that decide the telemetry environment (the look arguments need a receipt and
    do not touch the environment slice)."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    gen = spec["generatorArgs"]
    args = ["-Backend", spec["backends"][0], "-TelemetryArm", gen["telemetryArm"]]
    if gen.get("playbackProcessing"):
        args += ["-PlaybackProcessing", gen["playbackProcessing"]]
    return args


@requires_windows_pwsh
class HeavyArmKeepsFrameRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="heavy-frame-log-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.count = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, extra: list[str]) -> str:
        type(self).count += 1
        out = self.tmp / f"job-{type(self).count}.ps1"
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return lf(out.read_text(encoding="utf-8"))

    def job_envs(self, extra: list[str]) -> list[str]:
        """The `$envs` list the generated job passes to the smoke runner: the job's own slice, run by pwsh with the variables it reads stubbed."""
        text = self.generate(extra)
        start = text.index(SLICE_START)
        end = text.index(SLICE_END, start)
        script = self.tmp / "slice.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$TelemetryArm = '" + self.arm_in(text) + "'\n$reconDll = 'recon.dll'\n$pkgDir = 'pkg'\n$DisablePaintPerSubmit = $false\n"
            + text[start:end] + "\n$envs | ForEach-Object { $_ }\n",
            encoding="utf-8",
        )
        proc = run_pwsh(["-File", str(script)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    @staticmethod
    def arm_in(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("$TelemetryArm = '"):
                return line.split("'")[1]
        raise AssertionError("the generated job carries no $TelemetryArm assignment")

    def assert_rows_kept(self, extra: list[str], inherited: str | None) -> None:
        envs = self.job_envs(extra)
        self.assertIn("MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1", envs)
        seen = child_flag_value(envs, inherited)
        self.assertFalse(app_frame_rows_suppressed(seen), f"inherited {FLAG}={inherited!r} reaches the app as {seen!r}: every playback_smoke.frame row is suppressed")

    def assert_rows_suppressed(self, extra: list[str], inherited: str | None) -> None:
        seen = child_flag_value(self.job_envs(extra), inherited)
        self.assertTrue(app_frame_rows_suppressed(seen), f"LIGHT must keep suppressing the frame rows (inherited {inherited!r}, child {seen!r})")

    # --- sol r1 catalogue row: HEAVY receipt CPU spec + inherited disable flag 1 -> frame logging must remain enabled -------------------------------
    def test_heavy_receipt_cpu_spec_plus_inherited_disable_flag_1_must_retain_frame_rows(self) -> None:
        self.assert_rows_kept(spec_generator_args(NEW_SPEC), "1")

    def test_the_new_spec_still_asks_for_the_heavy_receipt_cpu_shape(self) -> None:
        self.assertEqual(spec_generator_args(NEW_SPEC), ["-Backend", "cpu", "-TelemetryArm", "HEAVY", "-PlaybackProcessing", "receipt"])

    # --- the class: every HEAVY job, whichever backend or processing mode the spec asks for ---------------------------------------------------------
    def test_heavy_cuda_job_retains_frame_rows_under_every_inherited_value(self) -> None:
        for inherited in (None, "1", "true", "on", "yes", "2"):
            with self.subTest(inherited=inherited):
                self.assert_rows_kept(["-TelemetryArm", "HEAVY"], inherited)

    def test_heavy_cpu_job_retains_frame_rows_under_every_inherited_value(self) -> None:
        for inherited in (None, "1", "true", "on", "yes", "2"):
            with self.subTest(inherited=inherited):
                self.assert_rows_kept(["-Backend", "cpu", "-TelemetryArm", "HEAVY"], inherited)

    def test_a_shared_heavy_spec_retains_frame_rows_when_the_flag_is_inherited(self) -> None:
        shared = [p for p in sorted(LEGS.glob("*.json")) if json.loads(p.read_text(encoding="utf-8"))["generatorArgs"].get("telemetryArm") == "HEAVY"]
        self.assertGreater(len(shared), 1, "expected the committed HEAVY specs")
        for path in shared[:3] + [NEW_SPEC]:
            with self.subTest(spec=path.name):
                self.assert_rows_kept(spec_generator_args(path), "1")

    # --- controls: LIGHT is unchanged ----------------------------------------------------------------------------------------------------------------
    def test_light_jobs_keep_suppressing_frame_rows_with_or_without_an_inherited_flag(self) -> None:
        for extra in (["-TelemetryArm", "LIGHT"], ["-Backend", "cpu", "-TelemetryArm", "LIGHT"]):
            for inherited in (None, "0", "1"):
                with self.subTest(extra=extra, inherited=inherited):
                    self.assert_rows_suppressed(extra, inherited)

    def test_light_job_environment_is_the_three_entries_it_always_had(self) -> None:
        envs = self.job_envs(["-TelemetryArm", "LIGHT"])
        self.assertEqual(
            [e for e in envs if e.startswith("MLVAPP_PLAYBACK_SMOKE_") or e.startswith("MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY")],
            ["MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1", "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1", "MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1"],
        )


class FlagRuleMirrorsTheApp(unittest.TestCase):
    """The test's copy of the app's flag rule is checked against the C++ it mirrors, so the two cannot drift apart unseen."""

    def test_environment_flag_enabled_off_values_are_the_ones_the_tests_assume(self) -> None:
        text = (ROOT / "platform" / "qt" / "MainWindow.cpp").read_text(encoding="utf-8")
        start = text.index("static bool environmentFlagEnabled( const char *name )")
        body = text[start:text.index("\n}\n", start)]
        for literal in ("0", "false", "off", "no"):
            self.assertIn(f'QByteArrayLiteral("{literal}")', body)
        self.assertIn("trimmed().toLower()", body)
        self.assertTrue(app_frame_rows_suppressed("1"))
        self.assertFalse(app_frame_rows_suppressed("0"))
        self.assertFalse(app_frame_rows_suppressed(None))


if __name__ == "__main__":
    unittest.main()
