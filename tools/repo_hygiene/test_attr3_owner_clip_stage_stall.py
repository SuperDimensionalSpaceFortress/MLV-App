"""BACHELOR-OWNER-CLIP-STAGE-STALL-1: an owner-clip attribution leg must never go silent in its
pre-launch phase, must read the clip in full exactly ONCE, and must size its timeouts from a
measured cold read rate.

WHAT WENT WRONG (r1c, r1e): on Bachelor a cold read of the 2.2 GB first part of the owner clip runs
at ~2 MB/s (independent of block size; the real-time scanner is on), and the generated leg job
read the clip three to four times before playback started (Get-FileHash in the owner-verify loop,
Get-FileHash again on the private link, then the smoke runner, then the app), printed nothing, and
was killed at its cap with an empty stdout.

WHAT THESE TESTS PIN. Behaviour (they EXECUTE pwsh): the large-block hashing reader is correct,
timed and traced; the trace writer appends flushed timestamped lines and never throws; the part
verifier has a screen-only mode that reads no content; the timeout derivation is arithmetic on a
size and a rate. Text (they read the generator's own template): every pre-launch step is bracketed
by a trace line in pipeline order, the template has no Get-FileHash left, the owner arm calls the
part verifier exactly twice -- once as the cheap length screen, once as the single full read on the
held link -- and the derived smoke-process timeout reaches the emitted job.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (
    ATTRIBUTION_GENERATOR,
    MODULE,
    PRESENCE_JOB_MODULE,
    STAGE_JOB_MODULE,
    _make_fixture_repo,
    _PwshCase,
    _run_pwsh_file,
    requires_git,
    requires_pwsh,
)

ROOT = Path(__file__).resolve().parents[2]
BACHELOR = ROOT / "tools" / "profiling" / "bachelor"
READ_RATE_MODULE = BACHELOR / "Attr3FootageReadRateJob.psm1"
READ_RATE_GENERATOR = BACHELOR / "attr3-footage-read-rate-job.ps1"
TRACE_FETCH_GENERATOR = BACHELOR / "attr3-trace-fetch-job.ps1"


def _template() -> str:
    """The emitted attribution job's template text, placeholders still in place."""
    text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
    start = text.index("$template = @'")
    end = text.index("\n'@", start)
    return text[start:end]


def _index_of(haystack: str, needle: str, start: int = 0) -> int:
    position = haystack.find(needle, start)
    assert position >= 0, f"template lacks {needle!r}"
    return position


@requires_pwsh
class LargeBlockHashReaderTests(_PwshCase):
    """Get-AttrCudaFileSha256Blocks: one sequential large-block read, timed, traced."""

    def _make_file(self, size: int) -> tuple[Path, str]:
        path = self.tmp / "sample.bin"
        data = os.urandom(size)
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def test_the_digest_equals_the_reference_and_the_result_reports_size_and_rate(self) -> None:
        path, expected = self._make_file(3 * 1024 * 1024 + 12345)
        proc = self.run_with_module(
            f"$r = Get-AttrCudaFileSha256Blocks -Path '{path}' -BlockBytes 1048576\n"
            "Write-Output ('SHA=' + $r.sha256)\n"
            "Write-Output ('BYTES=' + $r.bytes)\n"
            "Write-Output ('RATE_POSITIVE=' + ($r.mbPerSec -gt 0))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"SHA={expected}", proc.stdout)
        self.assertIn(f"BYTES={3 * 1024 * 1024 + 12345}", proc.stdout)
        self.assertIn("RATE_POSITIVE=True", proc.stdout)

    def test_an_empty_file_hashes_to_the_empty_digest(self) -> None:
        path, expected = self._make_file(0)
        proc = self.run_with_module(f"Write-Output ('SHA=' + (Get-AttrCudaFileSha256Blocks -Path '{path}').sha256)\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"SHA={expected}", proc.stdout)

    def test_it_traces_start_progress_and_done_lines(self) -> None:
        path, _ = self._make_file(3 * 1024 * 1024 + 1)
        trace = self.tmp / "logs" / "job.trace.txt"
        proc = self.run_with_module(
            f"[void](Get-AttrCudaFileSha256Blocks -Path '{path}' -BlockBytes 1048576 "
            f"-TracePath '{trace}' -Label 'unit' -ProgressBytes 1048576)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = trace.read_text(encoding="utf-8").splitlines()
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT[\d:.]+Z unit start bytes=3145729 blockBytes=1048576$")
        self.assertGreaterEqual(sum(" unit progress " in line for line in lines), 2)
        self.assertRegex(lines[-1], r" unit done bytes=3145729 seconds=[\d.]+ MBps=[\d.]+$")

    def test_a_missing_file_throws_and_does_not_echo_the_path(self) -> None:
        missing = self.tmp / "no-such-file-secret-name.bin"
        proc = self.run_with_module(
            f"try {{ [void](Get-AttrCudaFileSha256Blocks -Path '{missing}'); Write-Output 'NO_THROW' }} "
            "catch { Write-Output 'THREW' }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("THREW", proc.stdout)


@requires_pwsh
class TraceWriterTests(_PwshCase):
    def test_lines_are_appended_timestamped_and_one_per_call(self) -> None:
        trace = self.tmp / "logs" / "t.trace.txt"
        proc = self.run_with_module(
            f"Add-AttrCudaTraceLine -TracePath '{trace}' -Message 'first'\n"
            f"Add-AttrCudaTraceLine -TracePath '{trace}' -Message \"second`r`nstill second\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z first$")
        self.assertRegex(lines[1], r"Z second still second$")

    def test_a_blank_path_is_a_noop_and_an_unwritable_path_never_throws(self) -> None:
        blocker = self.tmp / "a-file"
        blocker.write_text("x", encoding="utf-8")
        proc = self.run_with_module(
            "Add-AttrCudaTraceLine -TracePath '' -Message 'nothing'\n"
            f"Add-AttrCudaTraceLine -TracePath '{blocker / 'sub' / 'x.trace.txt'}' -Message 'cannot be written'\n"
            "Write-Output 'SURVIVED'\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SURVIVED", proc.stdout)


@requires_pwsh
class FootagePartVerifierModesTests(_PwshCase):
    """Test-AttrCudaFootagePart: -LengthOnly reads no content; the full mode is one traced read."""

    def setUp(self) -> None:
        super().setUp()
        self.data = os.urandom(2 * 1024 * 1024 + 7)
        self.path = self.tmp / "part.bin"
        self.path.write_bytes(self.data)
        self.length = len(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.trace = self.tmp / "logs" / "v.trace.txt"

    def _status(self, extra: str, sha: str | None = None, length: int | None = None) -> str:
        proc = self.run_with_module(
            f"Write-Output ('STATUS=' + (Test-AttrCudaFootagePart -Path '{self.path}' "
            f"-ExpectedLength {self.length if length is None else length} "
            f"-ExpectedSha256 '{self.sha if sha is None else sha}' {extra}))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return re.search(r"STATUS=(\S+)", proc.stdout).group(1)

    def test_the_full_mode_passes_a_matching_hash_and_fails_a_wrong_one(self) -> None:
        self.assertEqual(self._status(""), "PASS")
        self.assertEqual(self._status("", sha="0" * 64), "SHA256_MISMATCH")

    def test_the_full_mode_traces_one_read_when_given_a_trace_path(self) -> None:
        self.assertEqual(self._status(f"-TracePath '{self.trace}' -TraceLabel 'ident'"), "PASS")
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(" ident start " in line for line in lines), 1)
        self.assertEqual(sum(" ident done " in line for line in lines), 1)

    def test_length_only_passes_without_reading_the_content_even_when_the_hash_is_wrong(self) -> None:
        self.assertEqual(self._status(f"-LengthOnly -TracePath '{self.trace}'", sha="0" * 64), "PASS")
        self.assertFalse(self.trace.exists(), "-LengthOnly must not hash: no content-read trace line may exist")

    def test_length_only_still_reports_a_length_mismatch_and_a_missing_file(self) -> None:
        self.assertEqual(self._status("-LengthOnly", length=self.length + 1), "LENGTH_MISMATCH")
        self.path.unlink()
        self.assertEqual(self._status("-LengthOnly"), "NOT_FOUND")


@requires_pwsh
class TimeBudgetDerivationTests(_PwshCase):
    """Get-AttrCudaLegTimeBudget: the timeouts are arithmetic on a size and a measured rate."""

    def _budget(self, args: str) -> dict:
        proc = self.run_with_module(f"Get-AttrCudaLegTimeBudget {args} | ConvertTo-Json -Compress\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_one_gib_at_two_mb_per_s_with_the_default_margin(self) -> None:
        budget = self._budget("-InputBytes 1073741824 -ColdReadMBps 2.0")
        self.assertEqual(budget["identityReadSec"], 768)  # 1024 MB / 2 MB/s * 1.5
        self.assertEqual(budget["smokeProcessTimeoutMs"], (768 + 60 + 40 + 3 + 30) * 1000)
        self.assertEqual(budget["jobTimeoutSec"], 768 + (768 + 60 + 40 + 3 + 30) + 600 + 300)

    def test_a_larger_clip_or_a_slower_rate_only_ever_lengthens_every_timeout(self) -> None:
        base = self._budget("-InputBytes 1073741824 -ColdReadMBps 2.0")
        bigger = self._budget("-InputBytes 2147483648 -ColdReadMBps 2.0")
        slower = self._budget("-InputBytes 1073741824 -ColdReadMBps 1.0")
        for other in (bigger, slower):
            for key in ("identityReadSec", "smokeProcessTimeoutMs", "jobTimeoutSec"):
                self.assertGreater(other[key], base[key], key)

    def test_a_fixture_with_no_owner_bytes_gets_only_the_fixed_allowances(self) -> None:
        budget = self._budget("-InputBytes 0")
        self.assertEqual(budget["identityReadSec"], 0)
        self.assertEqual(budget["smokeProcessTimeoutMs"], (60 + 40 + 3 + 30) * 1000)

    def test_a_clip_that_cannot_be_read_inside_the_smoke_ceiling_is_refused(self) -> None:
        proc = self.run_with_module(
            "try { [void](Get-AttrCudaLegTimeBudget -InputBytes 10737418240 -ColdReadMBps 2.0); Write-Output 'NO_THROW' } "
            "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
        )
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", proc.stdout)

    def test_the_default_rate_is_the_recorded_measurement_and_is_positive(self) -> None:
        proc = self.run_with_module("Write-Output ('B=' + (Get-AttrCudaLegTimeBudget -InputBytes 1048576).coldReadMBps)\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertGreater(float(re.search(r"B=([\d.]+)", proc.stdout).group(1)), 0.0)
        module_text = MODULE.read_text(encoding="utf-8")
        self.assertRegex(module_text, r"\$script:AttrCudaMeasuredColdReadMBps = [\d.]+")
        self.assertNotIn("PLACEHOLDER-UNTIL-MEASURED", module_text)


class OwnerClipTemplateTests(unittest.TestCase):
    """The attribution job's template (text): trace coverage, one read, derived timeout."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _template()

    def test_the_template_has_no_get_filehash_left(self) -> None:
        self.assertNotIn("Get-FileHash", self.template)

    def test_every_prelaunch_step_is_traced_in_pipeline_order(self) -> None:
        steps = [
            "job start id=",
            "step display-wake start",
            "step display-wake done",
            "step presentmon-verified",
            "step build-manifest-artifacts-verified",
            "step footage-verify start",
            "step footage-verify done",
            "step package-expand start",
            "step package-expand done",
            "step deploy start",
            "step deploy done",
            "step quiescence-check start",
            "step quiescence-check done",
            "step presentmon-spawn start",
            "step presentmon-spawn done",
            "step smoke-launch start",
            "step smoke-launch done",
        ]
        position = 0
        for step in steps:
            with self.subTest(step=step):
                position = _index_of(self.template, step, position)

    def test_the_trace_file_lives_under_the_agent_root_logs_named_by_the_job_id(self) -> None:
        self.assertIn('$Trace = Join-Path $Root "logs\\$JobId.trace.txt"', self.template)

    def test_the_part_verifier_is_called_twice_the_screen_before_the_link_and_the_one_read_after_the_handle(self) -> None:
        calls = [m.start() for m in re.finditer(r"Test-AttrCudaFootagePart -Path", self.template)]
        self.assertEqual(len(calls), 2, "screen + the single identity read; a third call is a re-read")
        screen, identity = calls
        screen_line = self.template[screen : self.template.index("\n", screen)]
        identity_line = self.template[identity : self.template.index("\n", identity)]
        self.assertIn("-LengthOnly", screen_line)
        self.assertNotIn("-LengthOnly", identity_line)
        self.assertIn("-TracePath $Trace", identity_line)
        link = _index_of(self.template, "New-AttrCudaOwnerFootageLink -Directory")
        held = _index_of(self.template, "Open-AttrCudaReadOnlyHandle -Path $linkPath")
        self.assertLess(screen, link)
        self.assertLess(held, identity, "the one full read must happen on the held link")

    def test_the_fixture_identity_check_is_one_traced_block_read(self) -> None:
        self.assertEqual(self.template.count("Get-AttrCudaFileSha256Blocks -Path $clipPath"), 1)

    def test_each_cached_artifact_is_hashed_once_and_its_digest_reused(self) -> None:
        self.assertIn("$manifestHashes[$check.label] = Get-Sha $check.path", self.template)
        self.assertIn("$cacheExeSha = $manifestHashes['exe']", self.template)
        self.assertIn("$cacheReconSha = $manifestHashes['dll']", self.template)
        self.assertNotIn("Get-Sha (Join-Path $Cache", self.template)

    def test_get_sha_is_the_traced_block_reader(self) -> None:
        body = self.template[_index_of(self.template, "function Get-Sha") :][:400]
        self.assertIn("Get-AttrCudaFileSha256Blocks", body)
        self.assertIn("-TracePath $Trace", body)

    def test_the_derived_smoke_process_timeout_reaches_the_runner_command(self) -> None:
        self.assertIn("$SmokeProcessTimeoutMs = __SMOKE_PROCESS_TIMEOUT_MS__", self.template)
        self.assertIn("-ProcessTimeoutMs $SmokeProcessTimeoutMs", self.template)
        generator = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("Get-AttrCudaLegTimeBudget", generator)
        self.assertIn("SMOKE_PROCESS_TIMEOUT_MS = [string]$timeBudget.smokeProcessTimeoutMs", generator)

    def test_the_embedders_carry_the_new_helpers_the_verifier_now_calls(self) -> None:
        for name in ("Add-AttrCudaTraceLine", "Get-AttrCudaFileSha256Blocks"):
            for source in (
                ATTRIBUTION_GENERATOR.read_text(encoding="utf-8"),
                PRESENCE_JOB_MODULE.read_text(encoding="utf-8"),
                STAGE_JOB_MODULE.read_text(encoding="utf-8"),
            ):
                self.assertIn(f"'{name}'", source)


@requires_pwsh
@requires_git
class OwnerClipEmittedJobTests(_PwshCase):
    """The REAL generator, run against the fixture repo: the derived timeout is baked in."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate(self, *extra: str) -> tuple[str, list[dict]]:
        out_file = self.staging / "job.ps1"
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' -BuildManifestSha256 '{'a' * 64}' "
            f"-ClipId 'tiny_dual_iso' -FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' -RepoRoot '{self.repo}' "
            + " ".join(extra)
            + " | ConvertTo-Json -Depth 6\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out_file.read_text(encoding="utf-8"), json.loads(proc.stdout)

    def test_a_fixture_job_bakes_the_fixed_allowance_timeout_and_reports_the_job_timeout(self) -> None:
        job_text, result = self._generate()
        self.assertNotIn("__SMOKE_PROCESS_TIMEOUT_MS__", job_text)
        self.assertIn(f"$SmokeProcessTimeoutMs = {(60 + 40 + 3 + 30) * 1000}", job_text)
        self.assertEqual(result["smokeProcessTimeoutMs"], (60 + 40 + 3 + 30) * 1000)
        self.assertEqual(result["recommendedJobTimeoutSec"], (60 + 40 + 3 + 30) + 600 + 300)

    def test_the_emitted_job_is_syntactically_valid_powershell(self) -> None:
        job_text, _ = self._generate()
        job = self.tmp / "emitted.ps1"
        job.write_text(job_text, encoding="utf-8")
        script = self.tmp / "parse.ps1"
        script.write_text(
            "$errs = $null; $toks = $null\n"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{job}', [ref]$toks, [ref]$errs)\n"
            "Write-Output ('PARSE_ERRORS=' + @($errs).Count)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertIn("PARSE_ERRORS=0", proc.stdout, proc.stdout + proc.stderr)


@requires_pwsh
class ReadRateAndTraceFetchGeneratorTests(_PwshCase):
    """The measurement job and the trace-fetch job: emitted, parseable, path-free."""

    def _parse_errors(self, job: Path) -> str:
        script = self.tmp / "parse.ps1"
        script.write_text(
            "$errs = $null; $toks = $null\n"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{job}', [ref]$toks, [ref]$errs)\n"
            "Write-Output ('PARSE_ERRORS=' + @($errs).Count)\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script).stdout

    def test_the_read_rate_job_is_built_from_parts_without_a_path_in_its_text(self) -> None:
        secret = "C:/secret-dir/owner-clip-part.bin"
        out = self.tmp / "out"
        script = self.tmp / "build.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{READ_RATE_MODULE}' -Force\n"
            f"$parts = @([pscustomobject]@{{ index = 0; path = '{secret}'; length = 1073741824; sha256 = ('a' * 64) }})\n"
            f"$r = New-Attr3FootageReadRateJob -ClipId 'X99-0001' -Parts $parts -OutDir '{out}' -RegionMB 16 | Where-Object {{ $_ -isnot [string] }}\n"
            "Write-Output ('JOB=' + $r.jobFile)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = Path(re.search(r"JOB=(.+)", proc.stdout).group(1).strip())
        text = job.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("secret-dir", text)
        self.assertIn(base64_of(secret), text)
        self.assertIn("$RegionBytes = [int64]16 * 1048576", text)
        self.assertNotIn("Get-CimInstance", text, "a CIM call took ~45 s on the measurement host")
        self.assertIn("PARSE_ERRORS=0", self._parse_errors(job))

    def test_the_trace_fetch_job_names_only_the_trace_directory(self) -> None:
        out = self.tmp / "fetch"
        script = self.tmp / "fetch.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{TRACE_FETCH_GENERATOR}' -OutDir '{out}' -TraceJobId 'playback-attr-3-cuda-abc-M16-1243-20260929-120000' -Tail 50\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = next(out.glob("attr3-trace-fetch-*.job.ps1"))
        text = job.read_text(encoding="utf-8")
        self.assertIn("'C:\\mlvtmp\\mlv-agent'", text)
        self.assertIn("'playback-attr-3-cuda-abc-M16-1243-20260929-120000'", text)
        self.assertIn("PARSE_ERRORS=0", self._parse_errors(job))

    def test_the_trace_fetch_generator_refuses_a_traversal_job_id(self) -> None:
        script = self.tmp / "fetch.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{TRACE_FETCH_GENERATOR}' -OutDir '{self.tmp / 'x'}' -TraceJobId '..\\..\\evil'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertNotEqual(proc.returncode, 0)


def base64_of(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


if __name__ == "__main__":
    unittest.main()
