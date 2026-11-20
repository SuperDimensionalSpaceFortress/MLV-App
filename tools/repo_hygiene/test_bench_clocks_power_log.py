"""Tests for BENCH-CLOCKS-POWER-LOG-1: the opt-in per-leg clocks / power / limit-flag sampler for venue legs (generatorArgs.clocksPower -> -ClocksPower).

WHAT IS PINNED
  * C1 byte identity, from this tree only: a copy of the generator with its one seam ($clocksPowerEmit) turned off emits, given -ClocksPower, the job
    without it, and a job without -ClocksPower carries no sampler text; stripping the bracketed regions gives the plain job back, with or without -UtilSample;
  * C2 presence: the five bracketed regions are there exactly once each, start ONE nvidia-smi process and ONE typeperf process per leg (never a process per
    sample: the only Process.Start calls are the two Start- helpers, outside any loop), and start after the pre-launch PresentMon stop sites;
  * C3 anchors: a template that lost the measured launch line (or its done line) is the typed DUAL_VENUE_CLOCKS_POWER_ANCHOR_MISSING refusal;
  * C4 the pure parsers and summaries ([N/A] never zero, a torn last row, rows outside the Play window, hex throttle masks, host-local time to UTC, a
    typeperf header read by counter name, a blank counter read), lifted from the generator by AST;
  * C5 fail open: a missing nvidia-smi, a missing typeperf, a source that is not on the host and a platform read that throws each give a typed
    UNAVAILABLE(reason) and never throw, never zero;
  * C6 forwarding: Invoke-VenueLeg passes ClocksPower only for a JSON true, never for "true", false or absent, and the interval only as an integer;
  * C7 schema: generatorArgs.clocksPower is a boolean and clocksPowerIntervalMs a bounded integer; the boolean list the runner guards matches the schema.
No test launches MLVApp (owner rule 2026-10-11): the generator only writes job text, and the runner runs against stubs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_dual_venue_evidence import (
    DV,
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    RunnerHarness,
    git,
    lf,
    requires_windows_pwsh,
    run_pwsh,
    run_pwsh_many,
)

SEAM_ON = re.compile(r"(?m)^\$clocksPowerEmit = \[bool\]\$ClocksPower(?=\r?$)")
SEAM_OFF = "$clocksPowerEmit = $false"
OPEN = "# BENCH-CLOCKS-POWER-LOG-1 >>>"
CLOSE = "# BENCH-CLOCKS-POWER-LOG-1 <<<"
UTIL_OPEN = "# CUDA-HEADROOM-UTIL-SAMPLER-1 >>>"
UTIL_CLOSE = "# CUDA-HEADROOM-UTIL-SAMPLER-1 <<<"
REGIONS = 5
LAUNCH_LINE = "Write-JobTrace 'step smoke-launch start (app launch + load + playback)'"
DONE_LINE = 'Write-JobTrace "step smoke-launch done rc=$smokeRc"'
CUDA_SHAPE = ["-Backend", "cuda", "-ScaleFactor", "1", "-ExpectedScaleRequest", "1", "-DisplayMode", "windowed", "-TelemetryArm", "LIGHT",
              "-CpuQuiescenceThresholdPercent", "95", "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-SwapInterval", "0",
              "-Unpaced", "-PresentTrace"]
CPU_SHAPE = ["-Backend", "cpu", *CUDA_SHAPE[2:]]
# the job builds the nvidia-smi command line at run time from the throttle field name (a driver may rename it), so the query is pinned in its pieces
GPU_QUERY = "--query-gpu=timestamp,clocks.gr,clocks.sm,clocks.mem,power.draw,enforced.power.limit,temperature.gpu,"
GPU_FORMAT = " --format=csv,noheader,nounits -i 0 -lms "


def region_re(open_: str, close: str) -> re.Pattern[str]:
    return re.compile(r"(?ms)^[ \t]*" + re.escape(open_) + r"\n.*?^[ \t]*" + re.escape(close) + r"\n")


def strip_regions(text: str, *pairs: tuple[str, str]) -> str:
    out = lf(text)
    for o, c in pairs:
        out = region_re(o, c).sub("", out)
    return out


@requires_windows_pwsh
class ClocksPowerGeneratorTests(unittest.TestCase):
    """Runs the generator for real against a sparse clone with header-only ~30 s fixtures (the device test_dual_venue_evidence uses)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = git("rev-parse", "HEAD")
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        source = GENERATOR.read_bytes().decode("utf-8")
        cls.seam_sites = len(SEAM_ON.findall(source))

        def tree_copy(name: str, text: str) -> Path:
            root = cls.tmp / name
            for part in ("profiling", "gates"):
                shutil.copytree(ROOT / "tools" / part, root / "tools" / part, ignore=shutil.ignore_patterns("__pycache__"))
            out = root / GENERATOR.relative_to(ROOT)
            out.write_bytes(text.encode("utf-8"))
            return out

        cls.off_generator = tree_copy("seam-off", SEAM_ON.sub(lambda _m: SEAM_OFF, source))
        cls.no_launch = tree_copy("no-launch", source.replace(LAUNCH_LINE, "Write-JobTrace 'step smoke-launch start (moved)'", 1))
        cls.no_done = tree_copy("no-done", source.replace(DONE_LINE, 'Write-JobTrace "step smoke-launch finished rc=$smokeRc"', 1))
        requests: list[tuple[str, Path, list[str]]] = [
            ("default", GENERATOR, []), ("cuda", GENERATOR, CUDA_SHAPE), ("cpu", GENERATOR, CPU_SHAPE),
            ("cp-default", GENERATOR, ["-ClocksPower"]), ("cp-cuda", GENERATOR, [*CUDA_SHAPE, "-ClocksPower"]),
            ("cp-cpu", GENERATOR, [*CPU_SHAPE, "-ClocksPower"]),
            ("cp-cuda-warm", GENERATOR, [*CUDA_SHAPE, "-ClocksPower", "-ClocksPowerWarmup", "-ClocksPowerIntervalMs", "2000"]),
            ("us-cuda", GENERATOR, [*CUDA_SHAPE, "-UtilSample"]), ("us-cp-cuda", GENERATOR, [*CUDA_SHAPE, "-UtilSample", "-ClocksPower"]),
            ("off-cuda", cls.off_generator, CUDA_SHAPE), ("off-cp-cuda", cls.off_generator, [*CUDA_SHAPE, "-ClocksPower"]),
            ("no-launch", cls.no_launch, [*CUDA_SHAPE, "-ClocksPower"]), ("no-done", cls.no_done, [*CUDA_SHAPE, "-ClocksPower"]),
            ("no-launch-plain", cls.no_launch, CUDA_SHAPE),
            ("bad-interval", GENERATOR, [*CUDA_SHAPE, "-ClocksPower", "-ClocksPowerIntervalMs", "100"]),
        ]
        commands = []
        cls.out = {}
        for key, script, extra in requests:
            out = cls.tmp / f"{key}.job.ps1"
            cls.out[key] = out
            commands.append(["-File", str(script), "-SourceCommit", cls.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                             "-FixtureSha256", "cd" * 32, "-RepoRoot", str(cls.repo), "-OutFile", str(out), *extra])
        cls.proc = dict(zip([k for k, _s, _e in requests], run_pwsh_many(commands)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def job(self, key: str) -> str:
        proc = self.proc[key]
        self.assertEqual(proc.returncode, 0, f"{key}: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
        return self.out[key].read_text(encoding="utf-8")

    def test_c1_the_seam_carries_every_emission_and_a_plain_job_has_no_sampler_text(self) -> None:
        self.assertEqual(self.seam_sites, 1, "the one seam $clocksPowerEmit = [bool]$ClocksPower")
        for shape in ("cuda",):
            on, off, plain = self.job(f"cp-{shape}"), self.job(f"off-cp-{shape}"), self.job(shape)
            self.assertNotEqual(on, plain)
            self.assertEqual(lf(off), lf(plain), "with the seam off, -ClocksPower emits the job without it")
        for key in ("default", "cuda", "cpu", "us-cuda"):
            text = self.job(key).lower()
            for token in ("clocks-power", "clocks_power", "bench-clocks", "typeperf", "gpu-clocks.csv", "cpu-clocks.csv", "clockspower"):
                self.assertNotIn(token, text, f"{key} carries {token} without -ClocksPower")

    def test_c1_stripping_the_regions_gives_the_plain_job_with_or_without_util_sample(self) -> None:
        self.assertEqual(strip_regions(self.job("cp-cuda"), (OPEN, CLOSE)), lf(self.job("cuda")))
        self.assertEqual(strip_regions(self.job("cp-cpu"), (OPEN, CLOSE)), lf(self.job("cpu")))
        both = strip_regions(self.job("us-cp-cuda"), (OPEN, CLOSE))
        self.assertEqual(both, lf(self.job("us-cuda")), "the two samplers share the launch anchors but never each other's text")

    def test_c2_five_regions_one_process_each_and_the_right_query(self) -> None:
        text = lf(self.job("cp-cuda"))
        self.assertEqual(len(region_re(OPEN, CLOSE).findall(text)), REGIONS)
        self.assertEqual(text.count(OPEN), REGIONS)
        self.assertEqual(text.count(CLOSE), REGIONS)
        self.assertEqual(text.count(GPU_QUERY), 1, "one nvidia-smi query, with the clocks/power/limit fields")
        self.assertEqual(text.count(GPU_FORMAT + "' + [string][int]$IntervalMs"), 1, "the interval is the job's $clocksPowerIntervalMs, not the util sampler's fixed 250")
        self.assertIn("'clocks_throttle_reasons.active'", text)
        self.assertIn("'clocks_event_reasons.active'", text, "the renamed-field retry")
        self.assertGreaterEqual(text.count("gpu-clocks.csv"), 1)
        self.assertGreaterEqual(text.count("cpu-clocks.csv"), 1)
        # one sampler process per leg: Process.Start only inside the two Start- helpers (each called once), never in a loop
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        self.assertEqual(len(re.findall(r"\[Diagnostics\.Process\]::Start\(", body)), 3, "Start-UtilSampleGpu's nvidia-smi, Start-ClocksPowerCpu's typeperf, and Invoke-ClocksPowerBounded's one short read (adversary de9ca8e7 A2), nothing else")
        self.assertEqual(len(re.findall(r"Start-ClocksPowerGpu \$clocksPowerBackend", body)), 1)
        self.assertEqual(len(re.findall(r"Start-ClocksPowerCpu ", body)), 1)
        self.assertNotRegex(body, r"(?is)\b(?:foreach|while|for)\b[^{]*\{[^}]*Start-(?:ClocksPower|UtilSample)(?:Gpu|Cpu)")
        # starts before the measured launch and after the keep-alive / session-locked PresentMon stop sites, stops right after the done line
        launch = text.index(LAUNCH_LINE)
        first_open = text.index(OPEN)
        self.assertLess(first_open, launch)
        self.assertLess(text.index(DONE_LINE), text.index(OPEN, launch))
        self.assertIn("step clocks-power start", text)
        self.assertIn("step clocks-power stop", text)

    def test_c2_the_warm_up_mark_and_the_interval_reach_the_job(self) -> None:
        text = lf(self.job("cp-cuda-warm"))
        self.assertIn("$clocksPowerWarmup = $true", text)
        self.assertIn("$clocksPowerIntervalMs = [int]2000", text)
        plain = lf(self.job("cp-cuda"))
        self.assertIn("$clocksPowerWarmup = $false", plain)
        self.assertIn("$clocksPowerIntervalMs = [int]1000", plain)

    def test_c2_the_summary_gains_the_block_and_the_status_scalar(self) -> None:
        text = lf(self.job("cp-cuda"))
        self.assertRegex(text, r"(?m)^    clocksPowerStatus = \$clocksPowerBlock\['status'\]$")
        self.assertRegex(text, r"(?m)^    clocksPower = \$clocksPowerBlock$")

    def test_a1_the_job_carries_the_clock_witness_and_hands_it_to_both_summaries(self) -> None:
        text = lf(self.job("cp-cuda"))
        for name in ("Get-ClocksPowerClockWhy", "Get-ClocksPowerNetStepMs"):
            self.assertEqual(len(re.findall(r"(?m)^function " + re.escape(name) + r"\b", text)), 1, f"{name} is spliced into the job once")
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        # the (UtcNow, monotonic) pair is read at the end of the start region and again at the start of the stop region
        self.assertGreaterEqual(len(re.findall(r"\[(?:System\.)?Diagnostics\.Stopwatch\]::GetTimestamp\(\)", body)), 2, "two (wall, monotonic) captures bracket the window")
        self.assertIn("Get-ClocksPowerNetStepMs", body)
        for summary, parsed in (("Get-ClocksPowerGpuSummary", "$clocksPowerGpuParsed"), ("Get-ClocksPowerCpuSummary", "$clocksPowerCpuParsed")):
            calls = [ln for ln in body.splitlines() if re.search(r"\b" + summary + r"\b", ln) and parsed in ln]
            self.assertEqual(len(calls), 1, f"{summary} is called once with the parsed rows")
            self.assertTrue(calls[0].rstrip().endswith("$clocksPowerClockCheck"), f"{summary} gets the clock check as its 5th argument: {calls[0].strip()}")

    def test_a2_no_pre_launch_read_is_unbounded(self) -> None:
        text = lf(self.job("cp-cuda"))
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        self.assertNotRegex(body, r"&\s*powercfg", "powercfg is read through Invoke-ClocksPowerBounded")
        self.assertNotRegex(body, r"&\s*\$TypeperfPath", "typeperf -qx is read through Invoke-ClocksPowerBounded")
        cim = [ln for ln in body.splitlines() if "Get-CimInstance" in ln]
        self.assertTrue(cim, "the platform read queries CIM")
        for ln in cim:
            self.assertIn("-OperationTimeoutSec", ln, f"a CIM query that can wait forever: {ln.strip()}")
        self.assertGreaterEqual(len(re.findall(r"Invoke-ClocksPowerBounded [\$']", body)), 2, "powercfg and the Energy Meter listing each go through it")

    def test_r6_a1_the_job_takes_bracketed_captures_and_builds_the_check_from_them(self) -> None:
        text = lf(self.job("cp-cuda"))
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        self.assertEqual(len(re.findall(r"(?m)^function Get-ClocksPowerClockCapture\b", text)), 1)
        self.assertEqual(len(re.findall(r"(?m)^function Get-ClocksPowerClockCheck\b", text)), 1)
        self.assertEqual(len(re.findall(r"\$clocksPowerClk1 = Get-ClocksPowerClockCapture\b", body)), 1)
        self.assertEqual(len(re.findall(r"\$clocksPowerClk2 = Get-ClocksPowerClockCapture\b", body)), 1)
        self.assertEqual(len(re.findall(r"Get-ClocksPowerClockCheck \$clocksPowerClk1 \$clocksPowerClk2 ", body)), 1)
        self.assertNotRegex(body, r"@\(\[DateTime\]::UtcNow\.Ticks, \[Diagnostics\.Stopwatch\]::GetTimestamp\(\)\)", "an unbracketed (wall, monotonic) pair: a deschedule between the two reads skews the capture")

    def test_r6b_a2_the_clock_captures_span_every_row_either_sampler_can_write(self) -> None:
        """adversary 16901aea A2: capture 1 was read AFTER the samplers started, so a wall step between a sampler's first row and that capture was in
        neither capture's span (net 0) while the pre-Play rows it shifted were selected as Play. Capture 1 now precedes the first sampler start and
        capture 2 follows both stops, so the net step covers every row a sampler can have written."""
        text = lf(self.job("cp-cuda"))
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        c1 = body.index("$clocksPowerClk1 = Get-ClocksPowerClockCapture")
        self.assertLess(c1, body.index("Start-ClocksPowerGpu $clocksPowerBackend"), "before nvidia-smi starts")
        self.assertLess(c1, body.index("Start-ClocksPowerCpu $clocksPowerTypeperf"), "before typeperf starts")
        c2 = body.index("$clocksPowerClk2 = Get-ClocksPowerClockCapture")
        self.assertGreater(c2, body.index("Stop-ClocksPowerAll $clocksPowerGpuStart"), "after both samplers are stopped")
        self.assertLess(c2, body.index("Get-UtilSamplePlayWindow $rawLog"), "and before the metrics region reads anything")

    def test_r6b_a3_the_job_reads_each_csv_once_parses_that_read_and_proves_the_copy(self) -> None:
        text = lf(self.job("cp-cuda"))
        metrics = [r for r in region_re(OPEN, CLOSE).findall(text) if "step clocks-power summary" in r][0]
        self.assertLess(metrics.index("Read-ClocksPowerSource"), metrics.index("Publish-ClocksPowerFiles"), "read first, then publish")
        self.assertLess(metrics.index("Publish-ClocksPowerFiles"), metrics.index("ConvertFrom-ClocksPowerGpuCsv"))
        self.assertNotIn("Read-UtilSampleText", metrics, "the summaries parse the one read, never a second read of the path")
        self.assertIn("ConvertFrom-ClocksPowerGpuCsv $clocksPowerGpuText", metrics)
        self.assertIn("ConvertFrom-ClocksPowerCpuCsv $clocksPowerCpuText", metrics)
        publish = re.search(r"(?ms)^function Publish-ClocksPowerFiles\b.*?^}", text)
        self.assertIsNotNone(publish)
        self.assertEqual(publish.group(0).count("Test-ClocksPowerPublishedCopy"), 2, "each side's copy is proved against its own read")
        self.assertEqual(publish.group(0).count("published-copy-differs"), 2)
        stop = re.search(r"(?ms)^function Stop-ClocksPowerAll\b.*?^}", text)
        self.assertIsNotNone(stop)
        self.assertNotIn("Dispose", stop.group(0), "the names stay held past the stop; Close-ClocksPowerHold releases them after the one read")

    def test_r7_a2_the_job_decides_on_a_surviving_pre_launch_child_before_either_sampler_starts(self) -> None:
        text = lf(self.job("cp-cuda"))
        body = "\n".join(region_re(OPEN, CLOSE).findall(text))
        guard = body.index("Test-ClocksPowerPreflightSurvivor $clocksPowerPlatform")
        self.assertEqual(len(re.findall(r"(?m)^function Test-ClocksPowerPreflightSurvivor\b", text)), 1)
        self.assertEqual(len(re.findall(r"(?m)^function Get-ClocksPowerRowGapStats\b", text)), 1)
        self.assertIn("preflight-child-survived", body)
        self.assertLess(body.index("$clocksPowerList = Get-ClocksPowerCounterList"), guard, "the Energy Meter listing is a pre-launch child too")
        self.assertLess(guard, body.index("$clocksPowerClk1 = Get-ClocksPowerClockCapture"), "decided before the first capture")
        self.assertLess(guard, body.index("$clocksPowerGpuStart = Start-ClocksPowerGpu $clocksPowerBackend"), "decided before nvidia-smi starts")
        self.assertLess(guard, body.index("$clocksPowerCpuStart = Start-ClocksPowerCpu"), "and before typeperf starts")

    def test_r6_a2_the_dgpu_pnp_read_is_bounded_on_the_clocks_path(self) -> None:
        text = lf(self.job("cp-cuda"))
        self.assertEqual(len(re.findall(r"(?m)^function Invoke-ClocksPowerDStateBounded\b", text)), 1)
        m = re.search(r"(?ms)^function Start-ClocksPowerGpu\b.*?^}", text)
        self.assertIsNotNone(m)
        self.assertIn("Invoke-ClocksPowerDStateBounded", m.group(0), "Start-ClocksPowerGpu reads the D-state through the bounded call, never & $DState directly")

    def test_r6_a3_the_files_are_published_before_the_block_names_them(self) -> None:
        text = lf(self.job("cp-cuda"))
        regions = [r for r in region_re(OPEN, CLOSE).findall(text) if "step clocks-power summary" in r]
        self.assertEqual(len(regions), 1)
        metrics = regions[0]
        self.assertLess(metrics.index("Publish-ClocksPowerFiles"), metrics.index("Get-ClocksPowerBlock"), "publish first, then name what was published")
        calls = [ln for ln in metrics.splitlines() if "= Get-ClocksPowerBlock" in ln]
        self.assertEqual(len(calls), 2)
        for ln in calls:
            self.assertTrue(ln.rstrip().endswith("$clocksPowerGpuFile $clocksPowerCpuFile"), ln.strip())
            self.assertNotIn("'gpu-clocks.csv'", ln)
        self.assertIn("Get-ClocksPowerRawFileName $clocksPowerGpuPublished 'gpu-clocks.csv'", metrics)
        self.assertIn("Get-ClocksPowerRawFileName $clocksPowerCpuPublished 'cpu-clocks.csv'", metrics)

    def test_c2_a_non_cuda_leg_keeps_the_wake_rule_and_a_cuda_leg_discloses_it(self) -> None:
        text = lf(self.job("cp-cpu"))
        self.assertIn("$clocksPowerBackend = 'cpu'", text)
        self.assertIn("$clocksPowerBackend = 'cuda'", lf(self.job("cp-cuda")))

    def test_c3_a_template_without_the_launch_or_done_line_is_a_typed_refusal(self) -> None:
        for key in ("no-launch", "no-done"):
            self.assertNotEqual(self.proc[key].returncode, 0, key)
            self.assertIn("DUAL_VENUE_CLOCKS_POWER_ANCHOR_MISSING", self.proc[key].stdout + self.proc[key].stderr)
        self.assertEqual(self.proc["no-launch-plain"].returncode, 0, "without -ClocksPower the missing anchor is not this feature's business")

    def test_c3_an_interval_below_the_floor_is_refused(self) -> None:
        self.assertNotEqual(self.proc["bad-interval"].returncode, 0)
        self.assertIn("ClocksPowerIntervalMs", self.proc["bad-interval"].stdout + self.proc["bad-interval"].stderr)

    def test_the_jobs_parse(self) -> None:
        script = self.tmp / "parse.ps1"
        script.write_text("$bad = 0\nforeach ($p in $args) { $e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
                          "if ($e.Count -gt 0) { $bad++; Write-Output ($p + ': ' + $e[0].Message) } }\nWrite-Output \"BAD=$bad\"\n", encoding="utf-8")
        paths = [str(self.out[k]) for k in ("cp-cuda", "cp-cpu", "cp-cuda-warm", "us-cp-cuda") if self.proc[k].returncode == 0]
        self.assertEqual(len(paths), 4, "every -ClocksPower generator run succeeded")
        proc = run_pwsh(["-File", str(script), *paths])
        self.assertIn("BAD=0", proc.stdout, proc.stdout + proc.stderr)


LIFT = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CP_GEN, [ref]$null, [ref]$null)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and ($n.Name -like '*ClocksPower*' -or $n.Name -like '*UtilSample*') }, $false)) {
    . ([scriptblock]::Create($f.Extent.Text))
}
"""

DRIVER = r"""
$r = [ordered]@{}
$cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
$ws = [DateTime]::new(2026, 11, 1, 14, 0, 0, 0, [DateTimeKind]::Utc); $we = $ws.AddMilliseconds(800)
$win = [ordered]@{ startUtc = $ws; endUtc = $we; clockWhy = $null }
$running = [ordered]@{ status = 'running'; reason = $null }
$gpuText = "2026/11/01 08:00:00.000, 1500, 1500, 6000, 50.00, 80.00, 60, 0x0000000000000000`n2026/11/01 08:00:00.250, 1400, 1400, 6000, 55.00, 80.00, 61, 0x0000000000000004`n" +
           "2026/11/01 08:00:00.500, [N/A], 1300, 6000, [N/A], 80.00, 62, 0x0000000000000001`n2026/11/01 08:00:00.750, 1200, 1200, 6000, 60.00, 80.00, 63, 0x0000000000000024`n" +
           "2026/11/01 08:00:01.000, 300, 300, 405, 10.00, 80.00, 50, 0x0000000000000040`n2026/11/01 08:00:01.2"
$g = ConvertFrom-ClocksPowerGpuCsv $gpuText $cst
$r['gpuParse'] = @{ rows = $g.rows.Count; torn = $g.tornTail; why = $g.why; firstUtc = $g.rows[0].utc.ToString('o'); naGr = ($null -eq $g.rows[2].grMhz); naPower = ($null -eq $g.rows[2].powerW); mask3 = [int64]$g.rows[3].throttleMask }
$ckGpu = [ordered]@{ intervalMs = 250; netStepMs = 0 }
$ckCpu = [ordered]@{ intervalMs = 1000; netStepMs = 0 }
$gs = Get-ClocksPowerGpuSummary $g $running $win $null $ckGpu
$r['gpuWin'] = $gs
$gn = Get-ClocksPowerGpuSummary $g $running $null
$r['gpuNoWin'] = @{ status = $gn.status; samples = $gn.samples; total = $gn.samplesTotal; limit = $gn.limitFlagsUnion }
$r['gpuAmbiguous'] = (Get-ClocksPowerGpuSummary (ConvertFrom-ClocksPowerGpuCsv "2026/11/01 08:00:00.000, 1, 1, 1, 1.0, 1.0, 1, 0x0`n2026/11/01 08:00:00.000, 2, 2, 2, 2.0, 2.0, 2, 0x0`n" $cst) $running $win).status
$r['gpuEmpty'] = (Get-ClocksPowerGpuSummary (ConvertFrom-ClocksPowerGpuCsv '' $cst) $running $win).status
$r['gpuMissingFile'] = (Get-ClocksPowerGpuSummary (ConvertFrom-ClocksPowerGpuCsv $null $cst) $running $win).status
$r['gpuGarbage'] = (Get-ClocksPowerGpuSummary (ConvertFrom-ClocksPowerGpuCsv "garbage`nmore garbage`n" $cst) $running $win).status
$r['gpuSkipped'] = (Get-ClocksPowerGpuSummary $g ([ordered]@{ status = 'skipped_wake_risk'; reason = 'dgpu-D3' }) $win).status
$r['gpuStartFailed'] = (Get-ClocksPowerGpuSummary $g ([ordered]@{ status = 'unavailable'; reason = 'nvidia-smi-missing' }) $win).status
$r['gpuEarlyExit'] = (Get-ClocksPowerGpuSummary $g $running $win ([ordered]@{ stopped = $true; exitedBeforeStop = $true; why = $null })).status
$allna = Get-ClocksPowerGpuSummary (ConvertFrom-ClocksPowerGpuCsv "2026/11/01 08:00:00.100, [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A]`n" $cst) $running $win $null $ckGpu
$r['gpuAllNa'] = @{ status = $allna.status; sm = $allna.smClockMhz; power = $allna.powerW; reasons = $allna.throttleReasonsUnion }
$r['gpuSummerUtc'] = (ConvertFrom-ClocksPowerGpuCsv "2026/07/01 08:00:00.000, 1, 1, 1, 1.0, 1.0, 1, 0x0`n" $cst).rows[0].utc.ToString('o')
$r['throttleNames'] = @{ zero = @(Get-ClocksPowerThrottleNames 0).Count; idle = (Get-ClocksPowerThrottleNames 1) -join ','; pcap = (Get-ClocksPowerThrottleNames 4) -join ','; all = (Get-ClocksPowerThrottleNames 0x1FF).Count; unknown = (Get-ClocksPowerThrottleNames 0x8000) -join ','; unknown62 = (Get-ClocksPowerThrottleNames 0x4000000000000000) -join ','; unknown63 = (Get-ClocksPowerThrottleNames ([int64]::MinValue)) -join ',' }
$cpuText = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\BACHELOR\Processor Information(_Total)\% Processor Performance`",`"\\BACHELOR\Processor Information(_Total)\Processor Frequency`",`"\\BACHELOR\Energy Meter(RAPL_Package0_PKG)\Power`"`n" +
           "`"11/01/2026 08:00:00.000`",`"100.000000`",`"2000.000000`",`"15000.000000`"`n`"11/01/2026 08:00:01.000`",`"150.000000`",`"2000.000000`",`"30000.000000`"`n" +
           "`"11/01/2026 08:00:02.000`",`"200.000000`",`"2000.000000`",`"45000.000000`"`n`"11/01/2026 08:00:03.000`",`" `",`"2000.000000`",`"45000.000000`"`n" +
           "`"11/01/2026 08:00:04.000`",`"50.000000`",`"2000.000000`",`"5000.000000`"`n`"11/01/2026 08:00:05.0"
$c = ConvertFrom-ClocksPowerCpuCsv $cpuText $cst
$r['cpuParse'] = @{ rows = $c.rows.Count; torn = $c.tornTail; why = $c.why; hasPkg = $c.hasPkgPower; firstUtc = $c.rows[0].utc.ToString('o'); blank = ($null -eq $c.rows[3].perfPct); eff1 = $c.rows[1].effectiveMhz; eff3 = $c.rows[3].effectiveMhz; pkg1 = $c.rows[1].pkgPowerW }
# adversary de9ca8e7 A1: typeperf rows are INTERVAL values (the row stamped T describes the second before T), so a row counts only when its whole interval
# lies inside the window: the window opens half a second before the first interval row's start here, so rows 1..3 count and row 0 (no known start) does not
$cw = [ordered]@{ startUtc = $ws.AddMilliseconds(-500); endUtc = $ws.AddMilliseconds(3500); clockWhy = $null }
$cs = Get-ClocksPowerCpuSummary $c $running $cw $null $ckCpu
$r['cpuWin'] = $cs
$noPkgText = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\B\Processor Information(_Total)\% Processor Performance`",`"\\B\Processor Information(_Total)\Processor Frequency`"`n`"11/01/2026 08:00:01.000`",`"150.0`",`"2000.0`"`n`"11/01/2026 08:00:02.000`",`"150.0`",`"2000.0`"`n"
$np = Get-ClocksPowerCpuSummary (ConvertFrom-ClocksPowerCpuCsv $noPkgText $cst) $running $cw $null $ckCpu
$r['cpuNoPkg'] = @{ status = $np.status; pkg = $np.pkgPowerW; eff = $np.effectiveMhz }
$r['cpuEmpty'] = (Get-ClocksPowerCpuSummary (ConvertFrom-ClocksPowerCpuCsv '' $cst) $running $cw).status
$r['cpuNoHeader'] = (Get-ClocksPowerCpuSummary (ConvertFrom-ClocksPowerCpuCsv "`"11/01/2026 08:00:01.000`",`"1`"`n" $cst) $running $cw).status
$r['cpuStartFailed'] = (Get-ClocksPowerCpuSummary $c ([ordered]@{ status = 'unavailable'; reason = 'typeperf-missing' }) $cw).status
$r['cpuNoWindow'] = (Get-ClocksPowerCpuSummary $c $running $null).status
$ser = Get-ClocksPowerSeries @(3, 1, 2, 4) 'no-rows'
$r['seriesEven'] = $ser
$r['seriesEmpty'] = Get-ClocksPowerSeries @($null, $null) 'all-not-supported'
$r['seriesBig'] = Get-ClocksPowerSeries @(1e308, 1e308) 'x'
$r['seriesOne'] = Get-ClocksPowerSeries @(7) 'x'
$blk = Get-ClocksPowerBlock $gs $cs ([ordered]@{ powerPlan = 'UNAVAILABLE(powercfg-failed)'; acState = 'ac'; display = 'UNAVAILABLE(cim-failed)' }) $win $true 1000 'gpu-clocks.csv' 'cpu-clocks.csv'
$r['block'] = $blk
$r['blockNone'] = Get-ClocksPowerBlock $null $null $null $null $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv'
$r['blockNoWindow'] = (Get-ClocksPowerBlock $gn $np $null $null $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv').status
$cnt = Resolve-ClocksPowerCounters @('Energy Meter(RAPL_Package0_CORE)\Power', 'Energy Meter(RAPL_Package0_PKG)\Power', 'Energy Meter(RAPL_Package0_PKG)\Energy', 'Energy Meter(RAPL_Package0_DRAM)\Power')
$r['counters'] = $cnt
$r['countersNone'] = Resolve-ClocksPowerCounters @()
$r['countersMulti'] = Resolve-ClocksPowerCounters @('Energy Meter(RAPL_Package0_PKG)\Power', 'Energy Meter(RAPL_Package1_PKG)\Power')
$r['planOk'] = ConvertFrom-ClocksPowerPowerCfg "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)`n"
$r['planBad'] = ConvertFrom-ClocksPowerPowerCfg "nonsense"
$r['acOn'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 2 })
$r['acOff'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 1 })
$r['acNoBattery'] = Get-ClocksPowerAcState @()
$r['acUnknown'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 99 })
$r['acCharging'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 6 })
$r['acPartial11'] = Get-ClocksPowerAcState @(@{ BatteryStatus = [uint16]11 })
$r['acFull3'] = Get-ClocksPowerAcState @(@{ BatteryStatus = [uint16]3 })
$r['acLow4'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 4 })
$r['acChargeLevelPlusDischarging'] = Get-ClocksPowerAcState @(@{ BatteryStatus = 11 }, @{ BatteryStatus = 1 })
$r['display'] = ConvertFrom-ClocksPowerDisplay @(@{ Name = 'NVIDIA GeForce RTX 3060 Laptop GPU'; CurrentHorizontalResolution = 2560; CurrentVerticalResolution = 1440; CurrentRefreshRate = 165 }, @{ Name = 'Intel(R) UHD'; CurrentHorizontalResolution = $null; CurrentVerticalResolution = $null; CurrentRefreshRate = $null })
$r['displayNone'] = ConvertFrom-ClocksPowerDisplay @()
$plat = Get-ClocksPowerPlatform
$r['platformKeys'] = @($plat.Keys)
$sg = Start-ClocksPowerGpu 'cuda' '' (Join-Path $env:CP_TMP 'g.csv') { @{ state = 'D0'; reason = $null } } 1000 'clocks_throttle_reasons.active'
$r['startGpuMissing'] = @{ status = $sg.status; reason = $sg.reason; proc = ($null -eq $sg.proc) }
$sc = Start-ClocksPowerCpu (Join-Path $env:CP_TMP 'no-such-typeperf.exe') (Join-Path $env:CP_TMP 'c.csv') 1000 @('\Processor Information(_Total)\% Processor Performance')
$r['startCpuMissing'] = @{ status = $sc.status; reason = $sc.reason; proc = ($null -eq $sc.proc) }
$sc2 = Start-ClocksPowerCpu 'typeperf.exe' (Join-Path $env:CP_TMP 'c2.csv') 1000 @()
$r['startCpuNoCounters'] = @{ status = $sc2.status; reason = $sc2.reason; proc = ($null -eq $sc2.proc) }
J $r
"""


@requires_windows_pwsh
class ClocksPowerPureHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-pure-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_c4_gpu_rows_na_is_null_never_zero_hex_mask_torn_tail_and_local_time(self) -> None:
        self.assertEqual(self.r["gpuParse"], {"rows": 5, "torn": True, "why": None, "firstUtc": "2026-11-01T14:00:00.0000000Z", "naGr": True,
                                              "naPower": True, "mask3": 36})
        self.assertEqual(self.r["gpuSummerUtc"], "2026-07-01T13:00:00.0000000Z", "host-local daylight time converts with its own offset")

    def test_c4_gpu_window_statistics_min_median_max_and_the_throttle_union(self) -> None:
        g = self.r["gpuWin"]
        self.assertEqual((g["status"], g["samples"], g["samplesTotal"]), ("ok", 4, 5))
        self.assertEqual(g["smClockMhz"], {"n": 4, "min": 1200.0, "median": 1350.0, "max": 1500.0})
        self.assertEqual(g["grClockMhz"], {"n": 3, "min": 1200.0, "median": 1400.0, "max": 1500.0}, "an [N/A] clock is left out, not counted as 0")
        self.assertEqual(g["powerW"], {"n": 3, "min": 50.0, "median": 55.0, "max": 60.0})
        self.assertEqual(g["powerLimitW"], {"n": 4, "min": 80.0, "median": 80.0, "max": 80.0})
        self.assertEqual(g["tempC"], {"n": 4, "min": 60.0, "median": 61.5, "max": 63.0})
        self.assertEqual(sorted(g["throttleReasonsUnion"]), ["GpuIdle", "SwPowerCap", "SwThermalSlowdown"], "HwThermalSlowdown was set only OUTSIDE the window")
        self.assertEqual(sorted(g["limitFlagsUnion"]), ["SwPowerCap", "SwThermalSlowdown"], "idle is a reason but not a limit")
        no_win = self.r["gpuNoWin"]
        self.assertEqual((no_win["status"], no_win["samples"], no_win["total"]), ("partial(no_window)", 5, 5))
        self.assertIn("HwThermalSlowdown", no_win["limit"], "with no window every row counts, and the status says so")

    def test_c4_gpu_typed_unavailable(self) -> None:
        self.assertEqual(self.r["gpuAmbiguous"], "unavailable(nvidia-smi-ambiguous)")
        self.assertEqual(self.r["gpuEmpty"], "unavailable(nvidia-smi-no-rows)")
        self.assertEqual(self.r["gpuMissingFile"], "unavailable(gpu-clocks-missing)")
        self.assertEqual(self.r["gpuGarbage"], "unavailable(nvidia-smi-unparseable)")
        self.assertEqual(self.r["gpuSkipped"], "skipped_wake_risk")
        self.assertEqual(self.r["gpuStartFailed"], "unavailable(nvidia-smi-missing)")
        self.assertEqual(self.r["gpuEarlyExit"], "unavailable(nvidia-smi-exited-early)")
        na = self.r["gpuAllNa"]
        self.assertTrue(na["status"].startswith("unavailable("), na)
        for key in ("sm", "power"):
            self.assertIsInstance(na[key], str, "an unsupported field is UNAVAILABLE(reason) text, never a number")
            self.assertTrue(na[key].startswith("UNAVAILABLE("), na)

    def test_c4_throttle_bit_names(self) -> None:
        t = self.r["throttleNames"]
        self.assertEqual((t["zero"], t["idle"], t["pcap"], t["all"]), (0, "GpuIdle", "SwPowerCap", 9))
        self.assertEqual(t["unknown"], "Unknown0x8000", "a bit the table does not know is named, never dropped")
        self.assertEqual(t["unknown62"], "Unknown0x4000000000000000", "adversary A2: bit 62 is named, not dropped by an off-by-two loop bound")
        self.assertEqual(t["unknown63"], "Unknown0x8000000000000000", "adversary A2: bit 63 (the sign bit) is named too")

    def test_c4_cpu_rows_header_mapping_effective_clock_and_package_power(self) -> None:
        p = self.r["cpuParse"]
        self.assertEqual((p["rows"], p["torn"], p["why"], p["hasPkg"], p["firstUtc"], p["blank"]), (5, True, None, True, "2026-11-01T14:00:00.0000000Z", True))
        self.assertEqual((p["eff1"], p["eff3"], p["pkg1"]), (3000.0, None, 30.0), "effective = Processor Frequency x % Processor Performance / 100; mW read as W")
        c = self.r["cpuWin"]
        self.assertEqual((c["status"], c["samples"], c["samplesTotal"]), ("ok", 3, 5))
        self.assertEqual(c["effectiveMhz"], {"n": 2, "min": 3000.0, "median": 3500.0, "max": 4000.0})
        self.assertEqual(c["perfPct"], {"n": 2, "min": 150.0, "median": 175.0, "max": 200.0})
        self.assertEqual(c["pkgPowerW"], {"n": 3, "min": 30.0, "median": 45.0, "max": 45.0})

    def test_c4_a_missing_cpu_source_is_unavailable_text_never_zero(self) -> None:
        np_ = self.r["cpuNoPkg"]
        self.assertEqual(np_["status"], "ok", "the clock is there even though the package-power counter is not")
        self.assertEqual(np_["pkg"], "UNAVAILABLE(energy-meter-counter-absent)")
        self.assertEqual(np_["eff"]["n"], 1)
        self.assertEqual(self.r["cpuEmpty"], "unavailable(typeperf-no-rows)")
        self.assertEqual(self.r["cpuNoHeader"], "unavailable(typeperf-unparseable)")
        self.assertEqual(self.r["cpuStartFailed"], "unavailable(typeperf-missing)")
        self.assertEqual(self.r["cpuNoWindow"], "partial(no_window)")

    def test_c4_series_statistics(self) -> None:
        self.assertEqual(self.r["seriesEven"], {"n": 4, "min": 1.0, "median": 2.5, "max": 4.0})
        self.assertEqual(self.r["seriesOne"], {"n": 1, "min": 7.0, "median": 7.0, "max": 7.0})
        self.assertEqual(self.r["seriesEmpty"], "UNAVAILABLE(all-not-supported)")
        self.assertEqual(self.r["seriesBig"], {"n": 2, "min": 1e308, "median": 1e308, "max": 1e308}, "adversary de9ca8e7 A5: the median of two huge finite values is not their overflowed sum")

    def test_c4_the_block_carries_window_warm_up_platform_and_files(self) -> None:
        b = self.r["block"]
        self.assertEqual(b["status"], "ok")
        self.assertEqual((b["warmup"], b["intervalMs"], b["windowStatus"]), (True, 1000, "ok"))
        self.assertEqual(b["window"]["startUtc"], "2026-11-01T14:00:00.0000000Z")
        self.assertEqual(b["platform"]["acState"], "ac")
        self.assertEqual(b["platform"]["powerPlan"], "UNAVAILABLE(powercfg-failed)")
        self.assertEqual(b["gpu"]["rawFile"], "gpu-clocks.csv")
        self.assertEqual(b["cpu"]["rawFile"], "cpu-clocks.csv")
        self.assertEqual(b["gpu"]["smClockMhz"]["median"], 1350.0)
        none = self.r["blockNone"]
        self.assertEqual(none["status"], "unavailable")
        self.assertTrue(none["reason"], "a typed reason")
        self.assertEqual(none["warmup"], False)
        self.assertEqual(self.r["blockNoWindow"], "partial")

    def test_c4_package_power_counter_selection(self) -> None:
        self.assertEqual(self.r["counters"], {"pkgPath": "Energy Meter(RAPL_Package0_PKG)\\Power", "reason": None})
        self.assertEqual(self.r["countersNone"], {"pkgPath": None, "reason": "energy-meter-counter-absent"})
        self.assertEqual(self.r["countersMulti"], {"pkgPath": None, "reason": "energy-meter-ambiguous"}, "two packages: no one counter is the CPU's package power")

    def test_c5_platform_power_plan_ac_state_and_display_mode(self) -> None:
        self.assertEqual(self.r["planOk"], {"name": "Balanced", "guid": "381b4222-f694-41f0-9685-ff5bb260df2e"})
        self.assertEqual(self.r["planBad"], "UNAVAILABLE(powercfg-unparseable)")
        self.assertEqual((self.r["acOn"], self.r["acOff"]), ("ac", "battery"))
        self.assertEqual(self.r["acNoBattery"], "UNAVAILABLE(no-battery-record)")
        self.assertEqual(self.r["acUnknown"], "UNAVAILABLE(battery-status-99)")
        # adversary de9ca8e7 A3: only 2 (AC) and 6-9 (charging) are AC-line evidence, only 1 (discharging) is battery evidence; a charge LEVEL (fully charged 3,
        # low 4, critical 5, partly charged 11) says nothing about the line, so it is named, never guessed
        self.assertEqual(self.r["acCharging"], "ac")
        for key, status in (("acPartial11", 11), ("acFull3", 3), ("acLow4", 4)):
            self.assertEqual(self.r[key], f"UNAVAILABLE(battery-status-{status})", key)
        self.assertEqual(self.r["acChargeLevelPlusDischarging"], "battery", "a discharging record is battery evidence even beside a charge-level record")
        self.assertEqual(self.r["display"], [{"adapter": "NVIDIA GeForce RTX 3060 Laptop GPU", "width": 2560, "height": 1440, "refreshHz": 165}])
        self.assertEqual(self.r["displayNone"], "UNAVAILABLE(no-active-display-mode)")
        self.assertEqual(sorted(self.r["platformKeys"]), ["acState", "display", "powerPlan"])

    def test_c5_a_missing_nvidia_smi_or_typeperf_starts_nothing_and_is_typed(self) -> None:
        self.assertEqual(self.r["startGpuMissing"], {"status": "unavailable", "reason": "nvidia-smi-missing", "proc": True})
        self.assertEqual(self.r["startCpuMissing"], {"status": "unavailable", "reason": "typeperf-missing", "proc": True})
        self.assertEqual(self.r["startCpuNoCounters"], {"status": "unavailable", "reason": "no-counters", "proc": True})


# adversary ebfb3c2b A1 (major, introduced): the clocks summaries used Test-UtilSampleClockSteady alone (stamps rise, no gap over 10 s), but this sampler has
# no monotonic witness (the util sampler's is its pwsh CPU child; nvidia-smi / typeperf are native). Rows whose stamps a host clock excursion bent
# (here 1000 ms nominal, stamped 18.100 / 19.100 / 19.600 / 21.100) passed as ok. The settled design: the summaries take a 5th argument $ClockCheck =
# @{ intervalMs; netStepMs }; a window with no usable check is sample-clock-unwitnessed; a net wall-vs-monotonic step over 250 ms, or any adjacent gap off
# the nominal interval by more than max(200, 0.4 x nominal) (the CPU nominal is the interval rounded UP to whole seconds: typeperf -si is in seconds),
# is sample-clock-unverified. Residual, disclosed in the code: an excursion shorter than the tolerance.
DRIVER_CLOCK = r"""
$cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
function T($sb) { try { & $sb } catch { 'ERR: ' + $_.Exception.Message } }
function St($o) { if ($o -is [string]) { $o } else { [string]$o.status } }
$r = [ordered]@{}
$running = [ordered]@{ status = 'running'; reason = $null }
$ws = [DateTime]::new(2026, 11, 1, 14, 0, 17, 0, [DateTimeKind]::Utc)
$win = [ordered]@{ startUtc = $ws; endUtc = $ws.AddSeconds(6); clockWhy = $null }
function Gpu([string[]]$stamps) {
    $t = ''
    foreach ($s in $stamps) { $t += "2026/11/01 08:00:$s, 1500, 1500, 6000, 50.00, 80.00, 60, 0x0000000000000000`n" }
    ConvertFrom-ClocksPowerGpuCsv $t $cst
}
function Cpu([string[]]$stamps) {
    $t = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\B\Processor Information(_Total)\% Processor Performance`",`"\\B\Processor Information(_Total)\Processor Frequency`",`"\\B\Energy Meter(RAPL_Package0_PKG)\Power`"`n"
    foreach ($s in $stamps) { $t += "`"11/01/2026 08:00:$s`",`"150.0`",`"2000.0`",`"30000.0`"`n" }
    ConvertFrom-ClocksPowerCpuCsv $t $cst
}
function Chk($interval, $net) { [ordered]@{ intervalMs = $interval; netStepMs = $net } }
$adv = @('18.100', '19.100', '19.600', '21.100')
$steady = @('18.100', '19.110', '20.090', '21.100')
$r['gpuAdv'] = St (T { Get-ClocksPowerGpuSummary (Gpu $adv) $running $win $null (Chk 1000 0) })
$r['gpuSteady'] = T { $s = Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 0); @{ status = St $s; samples = $s.samples } }
$r['gpuNet600'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 600) })
$r['gpuNetNeg600'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 ([double]-600)) })
$r['gpuNet250'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 250) })
$r['gpuNet251'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 251) })
$r['gpuNullCheck'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null $null })
$r['gpuNoNetStep'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null ([ordered]@{ intervalMs = 1000 }) })
$r['gpuNullNetStep'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $win $null (Chk 1000 $null) })
$r['gpuNoWindowNullCheck'] = St (T { Get-ClocksPowerGpuSummary (Gpu $steady) $running $null $null $null })
$r['gpuFast250'] = St (T { Get-ClocksPowerGpuSummary (Gpu @('18.000', '18.250', '18.500', '18.750')) $running $win $null (Chk 250 0) })
$r['gpuFast250Gap600'] = St (T { Get-ClocksPowerGpuSummary (Gpu @('18.000', '18.250', '18.850', '19.100')) $running $win $null (Chk 250 0) })
$r['cpuAdv'] = St (T { Get-ClocksPowerCpuSummary (Cpu $adv) $running $win $null (Chk 1000 0) })
# adversary de9ca8e7 A1: performance 50 % at second 0, 75 % at second 1, 100 % from second 2 on, frequency 2000 throughout, a row at every second 0..21,
# the Play window 0.5 s .. 20.5 s. Row 1 is the interval (0 s, 1 s] that straddles the window start, so it is not Play and must not pull the minimum to 1500
$t = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\B\Processor Information(_Total)\% Processor Performance`",`"\\B\Processor Information(_Total)\Processor Frequency`"`n"
for ($k = 0; $k -le 21; $k++) {
    $perfV = $(if ($k -eq 0) { '50.0' } elseif ($k -eq 1) { '75.0' } else { '100.0' })
    $t += ("`"11/01/2026 08:00:{0:00}.000`",`"{1}`",`"2000.0`"`n" -f $k, $perfV)
}
$straddleWin = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 1, 14, 0, 0, 500, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 1, 14, 0, 20, 500, [DateTimeKind]::Utc); clockWhy = $null }
$r['cpuStraddle'] = T { $s = Get-ClocksPowerCpuSummary (ConvertFrom-ClocksPowerCpuCsv $t $cst) $running $straddleWin $null (Chk 1000 0); @{ status = St $s; samples = $s.samples; effMin = $s.effectiveMhz.min; perfMin = $s.perfPct.min } }
$r['cpuRowFrom'] = T { $p = ConvertFrom-ClocksPowerCpuCsv $t $cst; @{ first = ($null -eq $p.rows[0].fromUtc); second = $p.rows[1].fromUtc.ToString('o'); secondUtc = $p.rows[1].utc.ToString('o') } }
$r['cpuSteady'] = T { $s = Get-ClocksPowerCpuSummary (Cpu $steady) $running $win $null (Chk 1000 0); @{ status = St $s; samples = $s.samples } }
$r['cpuNet600'] = St (T { Get-ClocksPowerCpuSummary (Cpu $steady) $running $win $null (Chk 1000 600) })
$r['cpuNullCheck'] = St (T { Get-ClocksPowerCpuSummary (Cpu $steady) $running $win $null $null })
$r['cpuNoWindowNullCheck'] = St (T { Get-ClocksPowerCpuSummary (Cpu $steady) $running $null $null $null })
$r['cpuInterval250OneSecondRows'] = St (T { Get-ClocksPowerCpuSummary (Cpu $steady) $running $win $null (Chk 250 0) })
$r['cpuInterval500HalfSecondGaps'] = St (T { Get-ClocksPowerCpuSummary (Cpu @('18.000', '18.500', '19.000', '19.500')) $running $win $null (Chk 500 0) })
$r['whyOk'] = T { Get-ClocksPowerClockWhy (Gpu $steady).rows (Chk 1000 0) }
$r['whyAdv'] = T { Get-ClocksPowerClockWhy (Gpu $adv).rows (Chk 1000 0) }
$r['whyNull'] = T { Get-ClocksPowerClockWhy (Gpu $steady).rows $null }
$r['whyBadInterval'] = T { Get-ClocksPowerClockWhy (Gpu $steady).rows (Chk 0 0) }
$r['whyOneRow'] = T { Get-ClocksPowerClockWhy (Gpu @('18.100')).rows (Chk 1000 0) }
$r['whyBackwards'] = T { Get-ClocksPowerClockWhy (Gpu @('19.100', '18.100')).rows (Chk 1000 0) }
$r['netStep0'] = T { Get-ClocksPowerNetStepMs 0 0 100000000 10000 1000 }
$r['netStepFwd'] = T { Get-ClocksPowerNetStepMs 0 0 106000000 10000 1000 }
$r['netStepBack'] = T { Get-ClocksPowerNetStepMs 0 0 97000000 10000 1000 }
J $r
"""


@requires_windows_pwsh
class ClocksPowerClockWitnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-clock-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER_CLOCK, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_a1_gpu_rows_a_clock_excursion_bent_are_unverified_not_ok(self) -> None:
        self.assertEqual(self.r["gpuAdv"], "unavailable(sample-clock-unverified)", "the adversary input: 18.100 / 19.100 / 19.600 / 21.100 against 1000 ms")
        self.assertEqual(self.r["gpuFast250Gap600"], "unavailable(sample-clock-unverified)", "a 600 ms gap against a 250 ms interval is off by 350 ms (> max(200, 100))")

    def test_a1_cpu_rows_a_clock_excursion_bent_are_unverified_not_ok(self) -> None:
        self.assertEqual(self.r["cpuAdv"], "unavailable(sample-clock-unverified)", "the CPU analog")

    def test_a1b_cpu_rows_are_interval_rows_and_a_row_straddling_the_window_start_is_not_play(self) -> None:
        self.assertEqual(self.r["cpuStraddle"], {"status": "ok", "samples": 19, "effMin": 2000.0, "perfMin": 100.0},
                         "the adversary input: the 50 % / 75 % intervals before Play must not reach the statistics (rows 2..20 are the 19 whole intervals inside 0.5 s..20.5 s)")
        self.assertEqual(self.r["cpuRowFrom"], {"first": True, "second": "2026-11-01T14:00:00.0000000Z", "secondUtc": "2026-11-01T14:00:01.0000000Z"},
                         "a row's interval starts at the previous row's stamp; the first row has no known start")

    def test_a1_a_net_wall_vs_monotonic_step_over_250_ms_is_unverified_either_way(self) -> None:
        for key in ("gpuNet600", "gpuNetNeg600", "gpuNet251", "cpuNet600"):
            self.assertEqual(self.r[key], "unavailable(sample-clock-unverified)", key)
        self.assertEqual(self.r["gpuNet250"], "ok", "250 ms is the bound itself: only a step OVER it refuses")

    def test_a1_a_window_with_no_usable_check_is_unwitnessed(self) -> None:
        for key in ("gpuNullCheck", "gpuNoNetStep", "gpuNullNetStep", "cpuNullCheck"):
            self.assertEqual(self.r[key], "unavailable(sample-clock-unwitnessed)", key)
        for key in ("gpuNoWindowNullCheck", "cpuNoWindowNullCheck"):
            self.assertEqual(self.r[key], "partial(no_window)", f"{key}: with no window nothing is cut by stamp, so no witness is asked for")

    def test_a1_a_steady_stream_with_a_clock_check_stays_ok(self) -> None:
        self.assertEqual(self.r["gpuSteady"], {"status": "ok", "samples": 4})
        self.assertEqual(self.r["cpuSteady"], {"status": "ok", "samples": 3}, "typeperf rows are interval rows: the first row (no known start) is not counted")
        self.assertEqual(self.r["gpuFast250"], "ok")
        self.assertEqual(self.r["cpuInterval250OneSecondRows"], "ok", "typeperf -si is whole seconds: the CPU nominal is the interval rounded UP, so 250 ms means 1 s")
        self.assertEqual(self.r["cpuInterval500HalfSecondGaps"], "unavailable(sample-clock-unverified)", "typeperf cannot sample at 500 ms: half-second gaps are not its cadence")

    def test_a1_the_clock_why_helper_and_the_net_step(self) -> None:
        self.assertIsNone(self.r["whyOk"])
        self.assertEqual(self.r["whyAdv"], "sample-clock-unverified")
        self.assertEqual(self.r["whyNull"], "sample-clock-unwitnessed")
        self.assertEqual(self.r["whyBadInterval"], "sample-clock-unwitnessed", "an interval that is not a positive number is no check")
        self.assertEqual(self.r["whyBackwards"], "sample-clock-unverified")
        self.assertIsNone(self.r["whyOneRow"], "one row has no gap to test; the window rule already needs a row inside it")
        self.assertAlmostEqual(self.r["netStep0"], 0.0)
        self.assertAlmostEqual(self.r["netStepFwd"], 600.0, msg="wall advanced 10.6 s while the monotonic clock advanced 10.0 s")
        self.assertAlmostEqual(self.r["netStepBack"], -300.0)


# adversary de9ca8e7 A2: the pre-launch reads (powercfg, the two CIM queries, `typeperf -qx "Energy Meter"`) ran synchronously with no bound, so a provider that
# never answers held the job before the measured launch and the "+45 s" job-timeout allowance bounded nothing. Each is now bounded and typed.
DRIVER_BOUNDED = r"""
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
function T($sb) { try { & $sb } catch { 'ERR: ' + $_.Exception.Message } }
$r = [ordered]@{}
$ping = Join-Path $env:SystemRoot 'System32\ping.exe'
$sw = [Diagnostics.Stopwatch]::StartNew()
$h = T { Invoke-ClocksPowerBounded $ping '-n 30 127.0.0.1' 800 }
$r['hangMs'] = $sw.ElapsedMilliseconds
$r['hang'] = @{ ok = $h.ok; timedOut = $h.timedOut; lines = @($h.lines).Count }
$ok = T { Invoke-ClocksPowerBounded $ping '-n 1 127.0.0.1' 15000 }
$r['quick'] = @{ ok = $ok.ok; timedOut = $ok.timedOut; hasLines = (@($ok.lines).Count -gt 0) }
$missing = T { Invoke-ClocksPowerBounded (Join-Path $env:CP_TMP 'no-such-tool.exe') '-x' 800 }
$r['missing'] = @{ ok = $missing.ok; timedOut = $missing.timedOut; lines = @($missing.lines).Count }
$sw.Restart()
$l = T { Get-ClocksPowerCounterList $ping 800 '-n 30 127.0.0.1' }
$r['listHangMs'] = $sw.ElapsedMilliseconds
$r['listHang'] = @{ lines = @($l.lines).Count; why = $l.why }
$r['listNone'] = T { $x = Get-ClocksPowerCounterList ''; @{ lines = @($x.lines).Count; why = $x.why } }
$r['listMissing'] = T { $x = Get-ClocksPowerCounterList (Join-Path $env:CP_TMP 'no-such-tool.exe'); @{ lines = @($x.lines).Count; why = $x.why } }
$r['listOk'] = T { $x = Get-ClocksPowerCounterList $ping 15000 '-n 1 127.0.0.1'; @{ hasLines = (@($x.lines).Count -gt 0); why = $x.why } }
J $r
"""


@requires_windows_pwsh
class ClocksPowerBoundedCallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-bounded-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER_BOUNDED, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_a2_a_tool_that_never_returns_is_killed_and_reported_not_waited_for(self) -> None:
        self.assertEqual(self.r["hang"], {"ok": False, "timedOut": True, "lines": 0})
        self.assertLess(self.r["hangMs"], 8000, "an 800 ms bound on a 29 s tool returns in seconds, not at the tool's pace")
        self.assertEqual(self.r["quick"], {"ok": True, "timedOut": False, "hasLines": True})
        self.assertEqual(self.r["missing"], {"ok": False, "timedOut": False, "lines": 0}, "a tool that cannot start is a failure, not a hang")

    def test_a2_the_energy_meter_listing_is_bounded_and_typed(self) -> None:
        self.assertEqual(self.r["listHang"], {"lines": 0, "why": "energy-meter-enumeration-timeout"})
        self.assertLess(self.r["listHangMs"], 8000)
        self.assertEqual(self.r["listNone"], {"lines": 0, "why": None}, "no typeperf path: nothing to list, and the start has its own typed reason")
        self.assertEqual(self.r["listMissing"], {"lines": 0, "why": "energy-meter-enumeration-failed"})
        self.assertEqual(self.r["listOk"], {"hasLines": True, "why": None})


# adversary 841c00e1 (pass 3; each finding is introduced, major):
#   A1  the (wall, monotonic) capture pair was read as two separate statements, so a deschedule between them skews that capture and can cancel a persistent
#       wall step: netStepMs came out 0 while the host clock had stepped -600 ms, and a row sampled after Play was counted in the Play window. Each capture is
#       now BRACKETED (monotonic, wall, monotonic): its monotonic stamp is the midpoint and its width is the bracket; a width over 25 ms is no witness.
#   A2  the dGPU PnP read (Get-PnpDevice / Get-PnpDeviceProperty) ran synchronously and unbounded inside the pre-launch reads, so the "50 s allowance" bounded
#       nothing: it is now run in its own runspace and abandoned at 4 s (pnp-read-timeout), like every other pre-launch read.
#   A3  the block named gpu-clocks.csv / cpu-clocks.csv as rawFile whether or not the copy happened (a Pub name that is an empty directory refuses the copy):
#       the files are published BEFORE the block is built, and rawFile names a file only when its copy succeeded (UNAVAILABLE(not-published) otherwise).
DRIVER_R6 = r"""
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
function T($sb) { try { & $sb } catch { 'ERR: ' + $_.Exception.Message } }
function St($o) { if ($o -is [string]) { $o } else { [string]$o.status } }
$r = [ordered]@{}
$utc = [TimeZoneInfo]::Utc
$running = [ordered]@{ status = 'running'; reason = $null }
# the adversary's rows: a 5 s interval, UTC stamps, the post-Play row at 21.700 carries a limit bit (mask 0x40)
$t = ''
foreach ($row in @(@('02.300', '0x0000000000000000'), @('06.700', '0x0000000000000000'), @('11.700', '0x0000000000000000'), @('16.700', '0x0000000000000000'), @('21.700', '0x0000000000000040'), @('26.700', '0x0000000000000000'))) {
    $t += "2026/11/01 12:00:$($row[0]), 1500, 1500, 6000, 50.00, 80.00, 60, $($row[1])`n"
}
$parsed = ConvertFrom-ClocksPowerGpuCsv $t $utc
$win = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 1, 12, 0, 2, 0, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 1, 12, 0, 22, 0, [DateTimeKind]::Utc); clockWhy = $null }
$f = [int64]10000000
$wall1 = [int64]639307512000000000
# A1: the first capture's wall read came 600 ms after its monotonic read (a deschedule); the wall clock then stepped -600 ms and stayed there
$descheduled = Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 600.0) @([int64]639307512294000000, [int64]300000000, 0.0) 5000 $f
$r['a1Descheduled'] = St (T { Get-ClocksPowerGpuSummary $parsed $running $win $null $descheduled })
$r['a1DescheduledCheckNull'] = ($null -eq $descheduled)
$stepped = Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 0.0) @([int64]639307512288000000, [int64]300000000, 0.0) 5000 $f
$r['a1TightStep'] = St (T { Get-ClocksPowerGpuSummary $parsed $running $win $null $stepped })
$steady = Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 5.0) @([int64]639307512294000000, [int64]300000000, 5.0) 5000 $f
$r['a1TightSteady'] = St (T { Get-ClocksPowerGpuSummary $parsed $running $win $null $steady })
$r['a1SteadyShape'] = @{ interval = $steady.intervalMs; net = $steady.netStepMs; width = $steady.captureWidthMs }
$r['a1Wide25'] = ($null -ne (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 25.0) @([int64]639307512294000000, [int64]300000000, 25.0) 5000 $f))
$r['a1Wide25p1'] = ($null -ne (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 25.1) @([int64]639307512294000000, [int64]300000000, 0.0) 5000 $f))
$r['a1SecondWide'] = ($null -ne (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 0.0) @([int64]639307512294000000, [int64]300000000, 600.0) 5000 $f))
$r['a1Missing'] = @(($null -eq (Get-ClocksPowerClockCheck $null $null 5000 $f)), ($null -eq (Get-ClocksPowerClockCheck @($wall1, [int64]6000000) @($wall1, [int64]6000000) 5000 $f)), ($null -eq (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 0.0) @($wall1, [int64]6000000, 0.0) 5000 0)))
$s0 = [Diagnostics.Stopwatch]::GetTimestamp()
$c = T { Get-ClocksPowerClockCapture }
$s1 = [Diagnostics.Stopwatch]::GetTimestamp()
$r['a1Capture'] = T { @{ n = @($c).Count; inBracket = ($c[1] -ge $s0 -and $c[1] -le $s1); wallNear = ([Math]::Abs([DateTime]::UtcNow.Ticks - [int64]$c[0]) -lt 100000000); width = ($c[2] -ge 0 -and $c[2] -lt 1000) } }
# A2: a PnP read that returns after 20 s is abandoned at 4 s, and nvidia-smi is not started
$sw = [Diagnostics.Stopwatch]::StartNew()
$slow = T { Start-ClocksPowerGpu 'cuda' '' (Join-Path $env:CP_TMP 'g-slow.csv') { Start-Sleep -Seconds 20; @{ state = 'D0'; reason = $null } } 1000 'clocks_throttle_reasons.active' }
$r['a2Ms'] = $sw.ElapsedMilliseconds
$r['a2Slow'] = T { @{ status = $slow.status; reason = $slow.reason; proc = ($null -eq $slow.proc) } }
$absent = T { Start-ClocksPowerGpu 'cuda' '' (Join-Path $env:CP_TMP 'g-absent.csv') { @{ state = 'absent'; reason = 'no-dgpu' } } 1000 'clocks_throttle_reasons.active' }
$r['a2Absent'] = T { @{ status = $absent.status; reason = $absent.reason } }
$thrown = T { Start-ClocksPowerGpu 'cuda' '' (Join-Path $env:CP_TMP 'g-throw.csv') { throw 'pnp exploded' } 1000 'clocks_throttle_reasons.active' }
$r['a2Thrown'] = T { @{ status = $thrown.status; reason = $thrown.reason } }
$real = T { Invoke-ClocksPowerDStateBounded ${function:Get-UtilSampleDgpuState} 30000 }
$r['a2Real'] = T { @{ hasState = ($real.ContainsKey('state')); hasReason = ($real.ContainsKey('reason')); notTimeout = ($real.reason -cne 'pnp-read-timeout') } }
# A3: a file is named only when its copy succeeded
$r['a3Names'] = @{ published = (Get-ClocksPowerRawFileName $true 'gpu-clocks.csv'); notPublished = (Get-ClocksPowerRawFileName $false 'gpu-clocks.csv') }
$blk = Get-ClocksPowerBlock $null $null $null $null $false 1000 (Get-ClocksPowerRawFileName $false 'gpu-clocks.csv') (Get-ClocksPowerRawFileName $true 'cpu-clocks.csv')
$r['a3Block'] = @{ gpu = $blk.gpu.rawFile; cpu = $blk.cpu.rawFile }
# A3 (pass 4, adversary 16901aea): the ONE read of a sampler CSV, and the proof that a published copy is those bytes
$srcPath = Join-Path $env:CP_TMP 'src.csv'
[IO.File]::WriteAllText($srcPath, "a,b`r`n1,2`r`n", [Text.UTF8Encoding]::new($true))
$srcHold = [IO.File]::Open($srcPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$owned = @{ owned = $true; hold = $srcHold }; $stopped = @{ stopped = $true }
$rd = Read-ClocksPowerSource $srcPath $owned $stopped
$r['a3Read'] = T { @{ text = $rd.text; shaIsFile = ($rd.sha256 -ceq (Get-FileHash -LiteralPath $srcPath -Algorithm SHA256).Hash.ToLowerInvariant()); bytesIsFile = ($rd.bytes -eq (Get-Item -LiteralPath $srcPath).Length) } }
$r['a3ReadRefused'] = @(($null -eq (Read-ClocksPowerSource $srcPath @{ owned = $false; hold = $srcHold } $stopped)), ($null -eq (Read-ClocksPowerSource $srcPath $owned @{ stopped = $false })),
    ($null -eq (Read-ClocksPowerSource $srcPath $owned $null)), ($null -eq (Read-ClocksPowerSource $srcPath $null $stopped)), ($null -eq (Read-ClocksPowerSource $srcPath @{ owned = $true } $stopped)))
$dstPath = Join-Path $env:CP_TMP 'dst.csv'
Copy-Item -LiteralPath $srcPath -Destination $dstPath -Force
$same = Test-ClocksPowerPublishedCopy $dstPath $rd
[IO.File]::WriteAllText($dstPath, "a,b`r`n1,3`r`n", [Text.UTF8Encoding]::new($true))
$differs = Test-ClocksPowerPublishedCopy $dstPath $rd
$r['a3Copy'] = @{ same = $same; differs = $differs; noRead = (Test-ClocksPowerPublishedCopy $dstPath $null); missing = (Test-ClocksPowerPublishedCopy (Join-Path $env:CP_TMP 'no-such.csv') $rd) }
# the name stays held until the caller releases it, and only for a stopped (or never started) writer
$h1 = [IO.File]::Open($srcPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
Close-ClocksPowerHold @{ hold = $h1 } @{ stopped = $false }
$heldWhileRunning = $h1.CanRead
Close-ClocksPowerHold @{ hold = $h1 } @{ stopped = $true }
$r['a3Hold'] = @{ heldWhileRunning = $heldWhileRunning; releasedWhenStopped = (-not $h1.CanRead) }
$h2 = [IO.File]::Open($srcPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
Close-ClocksPowerHold @{ hold = $h2 } @{ stopped = $false; why = 'not-started' }
$r['a3HoldNotStarted'] = (-not $h2.CanRead)
# A4 (pass 4 advisory): a plan name with its own parentheses keeps them; A6: a capture of nulls (or strings) is no capture
$r['a4Plan'] = @{ nested = (ConvertFrom-ClocksPowerPowerCfg "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced (Work))`n").name; crlf = (ConvertFrom-ClocksPowerPowerCfg "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)`r`n").name;
    guid = (ConvertFrom-ClocksPowerPowerCfg "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced (Work))").guid }
$r['a6Nulls'] = @(($null -eq (Get-ClocksPowerClockCheck @($null, $null, $null) @($null, $null, $null) 1000 $f)), ($null -eq (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, '0') @($wall1, [int64]6000000, 0.0) 1000 $f)),
    ($null -eq (Get-ClocksPowerClockCheck @($wall1, $null, 0.0) @($wall1, [int64]6000000, 0.0) 1000 $f)), ($null -ne (Get-ClocksPowerClockCheck @($wall1, [int64]6000000, 0.0) @($wall1, [int64]6000000, 0.0) 1000 $f)))
J $r
"""


@requires_windows_pwsh
class ClocksPowerPassThreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-r6-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER_R6, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_a1_a_wide_capture_bracket_is_no_witness_so_a_descheduled_capture_cannot_hide_a_step(self) -> None:
        self.assertTrue(self.r["a1DescheduledCheckNull"], "the adversary input: a 600 ms bracket on the first capture")
        self.assertEqual(self.r["a1Descheduled"], "unavailable(sample-clock-unwitnessed)", "the post-Play 21.700 row (a limit bit) must not reach an ok Play union")
        self.assertEqual(self.r["a1SecondWide"], False, "either capture")
        self.assertEqual(self.r["a1Wide25"], True, "25 ms is the bound itself, the util sampler's step limit")
        self.assertEqual(self.r["a1Wide25p1"], False)
        self.assertEqual(self.r["a1Missing"], [True, True, True], "no capture, a capture with no width, a non-positive frequency: no check")

    def test_a1_tight_brackets_still_see_a_step_and_a_steady_host_stays_ok(self) -> None:
        self.assertEqual(self.r["a1TightStep"], "unavailable(sample-clock-unverified)", "a -600 ms step with tight captures is the net-step rule")
        self.assertEqual(self.r["a1TightSteady"], "ok")
        self.assertEqual(self.r["a1SteadyShape"], {"interval": 5000, "net": 0.0, "width": 5.0})

    def test_a1_the_capture_is_bracketed_by_two_monotonic_reads(self) -> None:
        self.assertEqual(self.r["a1Capture"], {"n": 3, "inBracket": True, "wallNear": True, "width": True})

    def test_a2_a_pnp_read_that_never_returns_is_abandoned_and_typed(self) -> None:
        self.assertEqual(self.r["a2Slow"], {"status": "unavailable", "reason": "pnp-read-timeout", "proc": True})
        self.assertLess(self.r["a2Ms"], 16000, "a 20 s read bounded at 8 s returns in seconds, not at the read's pace")

    def test_a2_a_quick_or_failing_pnp_read_keeps_its_own_result(self) -> None:
        self.assertEqual(self.r["a2Absent"], {"status": "unavailable", "reason": "no-dgpu"})
        self.assertEqual(self.r["a2Thrown"], {"status": "unavailable", "reason": "pnp-read-failed"})
        self.assertEqual(self.r["a2Real"], {"hasState": True, "hasReason": True, "notTimeout": True})

    def test_a3_rawfile_names_a_file_only_when_its_copy_succeeded(self) -> None:
        self.assertEqual(self.r["a3Names"], {"published": "gpu-clocks.csv", "notPublished": "UNAVAILABLE(not-published)"})
        self.assertEqual(self.r["a3Block"], {"gpu": "UNAVAILABLE(not-published)", "cpu": "cpu-clocks.csv"})

    def test_r6b_a3_one_read_of_a_sampler_csv_feeds_both_the_summary_and_the_published_copy_check(self) -> None:
        self.assertEqual(self.r["a3Read"], {"text": "a,b\r\n1,2\r\n", "shaIsFile": True, "bytesIsFile": True}, "UTF-8 decoded with the BOM dropped; the hash and the length are the file's")
        self.assertEqual(self.r["a3ReadRefused"], [True] * 5, "not owned, writer not stopped, no stop record, no start record, no hold: nothing is read")
        self.assertEqual(self.r["a3Copy"], {"same": True, "differs": False, "noRead": False, "missing": False},
                         "a copy that hashes differently from the bytes the summary was parsed from is not published")

    def test_r6b_a4_a_power_plan_name_keeps_its_own_parentheses(self) -> None:
        self.assertEqual(self.r["a4Plan"], {"nested": "Balanced (Work)", "crlf": "Balanced", "guid": "381b4222-f694-41f0-9685-ff5bb260df2e"})

    def test_r6b_a6_a_capture_of_nulls_or_strings_is_no_witness(self) -> None:
        self.assertEqual(self.r["a6Nulls"], [True, True, True, True], "null elements and a string width give no check; a well-formed pair still does")

    def test_r6b_a3_the_names_stay_held_until_the_caller_has_read_them(self) -> None:
        self.assertEqual(self.r["a3Hold"], {"heldWhileRunning": True, "releasedWhenStopped": True})
        self.assertTrue(self.r["a3HoldNotStarted"])


# r7 (hub ruling 2026-11-20): the GPU witness refused EVERY window on the venue (r5 ON legs: 51 GPU rows against 93-94 CPU rows in the same legs, so the
# sampler cycles at about 1.8 s a row while the nominal interval is 1 s, and every adjacent gap broke max(200 ms, 0.4 x nominal)). The GPU gap test now
# judges the gaps against the OBSERVED median gap (a non-rising stamp, a gap over 10 s and a net clock step still refuse), and both summaries disclose the
# observed gaps (rowGapMs) so the cause is provable from summary.json on every run, a refused window included. Three cheap fixes of adversary pass 5:
#   A1  the net-step bound adds the capture width (a capture's monotonic stamp is the midpoint of its bracket): |net| + width > 250 refuses;
#   A2  a pre-launch child that survives the kill is reported (survived), typed (powercfg-survivor / energy-meter-enumeration-survivor), and the job starts
#       neither sampler beside it (unavailable(preflight-child-survived)); the kill waits are in the job-time budget (60 s);
#   A3  a CSV is read from the job's own held handle (no second path-based open), and the hold is closed only after a read that worked.
DRIVER_R7 = r"""
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
function T($sb) { try { & $sb } catch { 'ERR: ' + $_.Exception.Message } }
function St($o) { if ($o -is [string]) { $o } else { [string]$o.status } }
$r = [ordered]@{}
$cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
$running = [ordered]@{ status = 'running'; reason = $null }
$inv = [Globalization.CultureInfo]::InvariantCulture
function GpuAt([double[]]$secs) {
    $t = ''
    foreach ($s in $secs) { $t += ("2026/11/01 08:00:{0}, 1500, 1500, 6000, 50.00, 80.00, 60, 0x0000000000000000`n" -f $s.ToString('00.000', $inv)) }
    ConvertFrom-ClocksPowerGpuCsv $t $cst
}
function CpuAt([double[]]$secs) {
    $t = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\B\Processor Information(_Total)\% Processor Performance`",`"\\B\Processor Information(_Total)\Processor Frequency`"`n"
    foreach ($s in $secs) { $t += ("`"11/01/2026 08:00:{0}`",`"100.0`",`"2000.0`"`n" -f $s.ToString('00.000', $inv)) }
    ConvertFrom-ClocksPowerCpuCsv $t $cst
}
function Chk($interval, $net, $width) { $c = [ordered]@{ intervalMs = $interval; netStepMs = $net }; if ($null -ne $width) { $c['captureWidthMs'] = $width }; $c }
$win = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 1, 14, 0, 3, 0, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 1, 14, 0, 50, 0, [DateTimeKind]::Utc); clockWhy = $null }
# the measured venue cadence: nominal 1000 ms, a row about every 1.8 s (gaps 1.9, 1.9, 1.6 s repeating)
$cad = @(); for ($k = 0; $k -lt 30; $k++) { $cad += (0.1 + 1.8 * $k + (($k % 3) - 1) * 0.1) }
$g18 = T { Get-ClocksPowerGpuSummary (GpuAt $cad) $running $win $null (Chk 1000 0 5) }
$r['cad18'] = @{ status = St $g18; samples = $g18.samples; total = $g18.samplesTotal; gaps = $g18.rowGapMs }
$bent = @($cad | ForEach-Object -Begin { $i = 0 } -Process { $v = $_; if ($i -ge 15) { $v = $v - 0.8 }; $i++; $v })
$big = @($cad | ForEach-Object -Begin { $i = 0 } -Process { $v = $_; if ($i -ge 15) { $v = $v + 2.4 }; $i++; $v })
$back = @($cad | ForEach-Object -Begin { $i = 0 } -Process { $v = $_; if ($i -eq 10) { $v = $cad[8] - 0.05 }; $i++; $v })
$gb = T { Get-ClocksPowerGpuSummary (GpuAt $bent) $running $win $null (Chk 1000 0 5) }
$r['cad18Bent'] = @{ status = St $gb; gaps = $gb.rowGapMs }
$r['cad18Big'] = St (T { Get-ClocksPowerGpuSummary (GpuAt $big) $running $win $null (Chk 1000 0 5) })
$r['cad18Back'] = St (T { Get-ClocksPowerGpuSummary (GpuAt $back) $running $win $null (Chk 1000 0 5) })
$r['cad18Net'] = St (T { Get-ClocksPowerGpuSummary (GpuAt $cad) $running $win $null (Chk 1000 600 5) })
$r['cad18TwoGaps'] = St (T { Get-ClocksPowerGpuSummary (GpuAt @($cad[0], $cad[1], $cad[2])) $running $win $null (Chk 1000 0 5) })
$r['cad18ThreeGaps'] = St (T { Get-ClocksPowerGpuSummary (GpuAt @($cad[0], $cad[1], $cad[2], $cad[3])) $running $win $null (Chk 1000 0 5) })
$r['cad18Cpu'] = St (T { Get-ClocksPowerCpuSummary (CpuAt $cad) $running $win $null (Chk 1000 0 5) })
$c1 = T { $s = Get-ClocksPowerCpuSummary (CpuAt @(10, 11, 12, 13, 14)) $running $win $null (Chk 1000 0 5); @{ status = St $s; gaps = $s.rowGapMs } }
$r['cpuGaps'] = $c1
$r['gapsOneRow'] = T { (Get-ClocksPowerGpuSummary (GpuAt @(10)) $running $win $null (Chk 1000 0 5)).rowGapMs }
$r['gapsNotStarted'] = T { (Get-ClocksPowerGpuSummary $null $null $win).rowGapMs }
$r['gapStats'] = T { Get-ClocksPowerRowGapStats (GpuAt @(10, 11, 13, 16)).rows }
# A1: a persistent +260 ms step read as 240 by the midpoints, with 20 ms brackets
$f = [int64]10000000
$w1 = [int64]639307728000200000
$steady5 = (GpuAt @(18.1, 23.1, 28.1, 33.1)).rows
function Clk([int64]$wallDeltaTicks, [double]$width) { Get-ClocksPowerClockCheck @($w1, [int64]100000, $width) @(($w1 + $wallDeltaTicks), [int64]300100000, $width) 5000 $f }
$r['a1Adv'] = T { Get-ClocksPowerClockWhy $steady5 (Clk 302400000 20.0) }
$r['a1AdvNeg'] = T { Get-ClocksPowerClockWhy $steady5 (Clk 297600000 20.0) }
$r['a1Boundary'] = T { Get-ClocksPowerClockWhy $steady5 (Clk 302450000 5.0) }
$r['a1Over'] = T { Get-ClocksPowerClockWhy $steady5 (Clk 302460000 5.0) }
$r['a1NoWidth250'] = T { Get-ClocksPowerClockWhy $steady5 (Chk 5000 250 $null) }
$r['a1NoWidth251'] = T { Get-ClocksPowerClockWhy $steady5 (Chk 5000 251 $null) }
$r['a1ShapeAdv'] = T { $c = Clk 302400000 20.0; @{ net = $c.netStepMs; width = $c.captureWidthMs } }
# A2: a tool that survives its kill
$ping = Join-Path $env:SystemRoot 'System32\ping.exe'
$sv = T { Invoke-ClocksPowerBounded $ping '-n 30 127.0.0.1' 800 { param($p) throw 'kill refused' } }
$r['a2Survivor'] = T { @{ timedOut = $sv.timedOut; survived = $sv.survived; hasPid = ([int]$sv.pid -gt 0) } }
try { if ([int]$sv.pid -gt 0) { Stop-Process -Id ([int]$sv.pid) -Force -ErrorAction SilentlyContinue } } catch { }
$kd = T { Invoke-ClocksPowerBounded $ping '-n 30 127.0.0.1' 800 }
$r['a2Killed'] = T { @{ timedOut = $kd.timedOut; survived = $kd.survived } }
$qk = T { Invoke-ClocksPowerBounded $ping '-n 1 127.0.0.1' 15000 }
$r['a2Quick'] = T { @{ ok = $qk.ok; timedOut = $qk.timedOut; survived = $qk.survived } }
$cl = T { Get-ClocksPowerCounterList $ping 800 '-n 30 127.0.0.1' { param($p) throw 'kill refused' } }
$r['a2List'] = T { @{ lines = @($cl.lines).Count; why = $cl.why } }
$r['a2Preflight'] = T { @((Test-ClocksPowerPreflightSurvivor @{ powerPlan = 'UNAVAILABLE(powercfg-survivor)' } @{ why = $null }), (Test-ClocksPowerPreflightSurvivor @{ powerPlan = 'UNAVAILABLE(powercfg-timeout)' } @{ why = 'energy-meter-enumeration-survivor' }),
    (Test-ClocksPowerPreflightSurvivor @{ powerPlan = 'UNAVAILABLE(powercfg-timeout)' } @{ why = 'energy-meter-enumeration-timeout' }), (Test-ClocksPowerPreflightSurvivor $null $null), (Test-ClocksPowerPreflightSurvivor @{ powerPlan = [ordered]@{ name = 'Balanced'; guid = 'x' } } @{ why = $null })) }
# A3: the bytes come from the held handle, never from the path
$p3 = Join-Path $env:CP_TMP 'a3-held.csv'
$foreign = Join-Path $env:CP_TMP 'a3-foreign.csv'
[IO.File]::WriteAllText($p3, "orig,1`r`n", [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($foreign, "foreign,9`r`n", [Text.UTF8Encoding]::new($false))
$hold = [IO.File]::Open($p3, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$st = [ordered]@{ owned = $true; hold = $hold }; $stopped = @{ stopped = $true }
$rd = Read-ClocksPowerSource $foreign $st $stopped
$r['a3FromHold'] = T { @{ text = $rd.text; bytes = $rd.bytes; shaIsOrig = ($rd.sha256 -ceq (Get-FileHash -LiteralPath $p3 -Algorithm SHA256).Hash.ToLowerInvariant()) } }
$r['a3NoHold'] = ($null -eq (Read-ClocksPowerSource $foreign ([ordered]@{ owned = $true; hold = $null }) $stopped))
$closed = [IO.File]::Open($p3, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite); $closed.Dispose()
$r['a3ClosedHold'] = ($null -eq (Read-ClocksPowerSource $foreign ([ordered]@{ owned = $true; hold = $closed }) $stopped))
# a failed first read leaves the hold open, the path cannot be replaced meanwhile, and the retry reads the same handle
$lk = [IO.File]::Open($p3, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
$lk.Lock(0, 4)
$first = Read-ClocksPowerSource $p3 $st $stopped
$r['a3First'] = ($null -eq $first)
$r['a3HoldCanReadAfterFail'] = $hold.CanRead
$replaced = $true
try { Move-Item -LiteralPath $foreign -Destination $p3 -Force -ErrorAction Stop } catch { $replaced = $false }
$r['a3Replaced'] = $replaced
$lk.Unlock(0, 4); $lk.Dispose()
$second = Read-ClocksPowerSource $p3 $st $stopped
$r['a3Second'] = T { @{ text = $second.text } }
$hold.Dispose()
J $r
"""


@requires_windows_pwsh
class ClocksPowerRoundSevenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-r7-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER_R7, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None
        cls.text = lf(GENERATOR.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_r7_gpu_rows_at_the_measured_1_8_s_cadence_are_judged_against_their_own_median_gap(self) -> None:
        g = self.r["cad18"]
        self.assertEqual((g["status"], g["total"]), ("ok", 30), "30 rows at about 1.8 s against a 1000 ms nominal: the venue's measured cadence is not a clock excursion")
        self.assertGreaterEqual(g["samples"], 20)
        self.assertEqual((g["gaps"]["n"], g["gaps"]["minMs"], g["gaps"]["medianMs"], g["gaps"]["maxMs"]), (29, 1600.0, 1900.0, 1900.0))
        self.assertEqual(self.r["cad18Cpu"], "unavailable(sample-clock-unverified)", "the CPU side keeps its nominal-interval rule (typeperf -si is whole seconds)")

    def test_r7_an_excursion_a_gap_a_backwards_stamp_and_a_net_step_still_refuse_at_that_cadence(self) -> None:
        self.assertEqual(self.r["cad18Bent"]["status"], "unavailable(sample-clock-unverified)", "one 0.8 s gap among 1.9 s gaps is off the median by 1.1 s (> 0.76 s)")
        self.assertEqual(self.r["cad18Big"], "unavailable(sample-clock-unverified)")
        self.assertEqual(self.r["cad18Back"], "unavailable(sample-clock-unverified)", "a stamp that does not rise")
        self.assertEqual(self.r["cad18Net"], "unavailable(sample-clock-unverified)", "a 600 ms net step")
        self.assertEqual(self.r["cad18TwoGaps"], "unavailable(sample-clock-unverified)", "fewer than three gaps have no observed cadence: the nominal interval judges them")
        self.assertEqual(self.r["cad18ThreeGaps"], "ok", "three gaps are the first with an observed median")

    def test_r7_both_summaries_disclose_the_observed_row_gaps_even_when_the_window_is_refused(self) -> None:
        self.assertEqual(self.r["cad18Bent"]["gaps"]["n"], 29)
        self.assertEqual(self.r["cad18Bent"]["gaps"]["minMs"], 800.0, "the refused window still carries what the stamps said")
        self.assertEqual(self.r["cpuGaps"], {"status": "ok", "gaps": {"n": 4, "minMs": 1000.0, "medianMs": 1000.0, "maxMs": 1000.0}})
        self.assertEqual(self.r["gapsOneRow"], "UNAVAILABLE(fewer-than-two-rows)")
        self.assertEqual(self.r["gapsNotStarted"], "UNAVAILABLE(start-not-reached)")
        self.assertEqual(self.r["gapStats"], {"n": 3, "minMs": 1000.0, "medianMs": 2000.0, "maxMs": 3000.0})

    def test_r7_a1_the_net_step_bound_includes_the_capture_width(self) -> None:
        self.assertEqual(self.r["a1ShapeAdv"], {"net": 240.0, "width": 20.0})
        self.assertEqual(self.r["a1Adv"], "sample-clock-unverified", "the adversary input: net 240 with 20 ms brackets reads as 260 against a real +260 ms step")
        self.assertEqual(self.r["a1AdvNeg"], "sample-clock-unverified", "either sign")
        self.assertIsNone(self.r["a1Boundary"], "net 245 + width 5 is 250, the bound itself")
        self.assertEqual(self.r["a1Over"], "sample-clock-unverified")
        self.assertIsNone(self.r["a1NoWidth250"], "no width is width 0: the older 250 stays ok")
        self.assertEqual(self.r["a1NoWidth251"], "sample-clock-unverified")

    def test_r7_a2_a_tool_that_survives_the_kill_is_reported_and_typed(self) -> None:
        self.assertEqual(self.r["a2Survivor"], {"timedOut": True, "survived": True, "hasPid": True})
        self.assertEqual(self.r["a2Killed"], {"timedOut": True, "survived": False}, "an ordinary kill leaves no survivor")
        self.assertEqual(self.r["a2Quick"], {"ok": True, "timedOut": False, "survived": False})
        self.assertEqual(self.r["a2List"], {"lines": 0, "why": "energy-meter-enumeration-survivor"})
        self.assertEqual(self.r["a2Preflight"], [True, True, False, False, False])

    def test_r7_a2_the_job_starts_neither_sampler_beside_a_survivor_and_budgets_the_kill_waits(self) -> None:
        bounded = re.search(r"(?ms)^function Invoke-ClocksPowerBounded\b.*?^}", self.text)
        self.assertIsNotNone(bounded)
        self.assertIn("Kill($true)", bounded.group(0), "the tree is killed, then the process")
        platform = re.search(r"(?ms)^function Get-ClocksPowerPlatform\b.*?^}", self.text)
        self.assertIsNotNone(platform)
        self.assertIn("UNAVAILABLE(powercfg-survivor)", platform.group(0))
        self.assertEqual(len(re.findall(r"(?m)^function Test-ClocksPowerPreflightSurvivor\b", self.text)), 1)
        self.assertEqual(self.text.count("${function:Test-ClocksPowerPreflightSurvivor}"), 1, "the job carries the helper")
        self.assertEqual(self.text.count("${function:Get-ClocksPowerRowGapStats}"), 1, "the job carries the gap helper")
        self.assertIn("$timeBudget['clocksPowerSec'] = 60", self.text)

    def test_r7_the_generator_names_no_engine_type_the_publish_write_scan_reads_as_a_session_carrier(self) -> None:
        """r6 added an in-process runspace for the dGPU read, and a powershell or scriptblock type token anywhere in the generator makes the splice scan
        (attr3_publish_write_scan.ps1, Test-TreeNamesSessionCarrier) treat every unfoldable member name as a session-state reach: five tests of
        test_attr3_spliced_body_scan.py went red (110 passed on the base). The scan's own pattern is mirrored here."""
        hits = re.findall(r"(?i)\[(?:[a-z_][a-z0-9_]*\.)*(?:runspace|runspacefactory|powershell|sessionstate|sessionstateproxy|psvariable|psvariableintrinsics|engineintrinsics|psmoduleinfo|commandinfo|functioninfo|scriptblock|pshost|pscmdlet)\]", self.text)
        self.assertEqual(hits, [], "an engine type token in the generator trips the splice scan's unproven-binding")

    def test_r7_a3_a_csv_is_read_from_the_held_handle_never_from_the_path(self) -> None:
        self.assertEqual(self.r["a3FromHold"], {"text": "orig,1\r\n", "bytes": 8, "shaIsOrig": True}, "the path argument names a foreign file: the bytes are the hold's")
        self.assertTrue(self.r["a3NoHold"], "no hold: nothing is read, whatever the path holds")
        self.assertTrue(self.r["a3ClosedHold"], "a released hold is not a source")
        self.assertTrue(self.r["a3First"], "a byte-range lock makes the first read fail")
        self.assertTrue(self.r["a3HoldCanReadAfterFail"], "a failed read leaves the hold open")
        self.assertFalse(self.r["a3Replaced"], "the held name cannot be replaced while the hold is open")
        self.assertEqual(self.r["a3Second"], {"text": "orig,1\r\n"}, "the retry reads the same handle and gets the original bytes")

    def test_r7_a3_the_hold_is_closed_only_after_a_read_that_worked(self) -> None:
        sites = [ln.strip() for ln in self.text.splitlines() if "Close-ClocksPowerHold $clocksPower" in ln]
        self.assertGreaterEqual(len(sites), 4, "two in the metrics region, two in Publish-ClocksPowerFiles")
        for ln in sites:
            self.assertRegex(ln, r"if \(\$null -ne \$(?:script:)?clocksPower(?:Gpu|Cpu)Read\) \{ Close-ClocksPowerHold ", ln)


# Round 8 (DESCOPE, hub ruling 2026-11-20T15:50Z): the GPU half is an ADVISORY disclosure. On the venue the GPU witness refused every window (0 of 51
# samples in r5 and r7), so the GPU block can never set clocksPowerStatus, never names itself in the reason and never gates a leg; the status
# reflects the CPU half only. The GPU raw file stays published (it is the evidence BENCH-GPU-CLOCKS-FLUSH-1 needs).
DRIVER_R8 = r"""
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
function T($sb) { try { & $sb } catch { 'ERR: ' + $_.Exception.Message } }
$r = [ordered]@{}
$cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
$running = [ordered]@{ status = 'running'; reason = $null }
$inv = [Globalization.CultureInfo]::InvariantCulture
function GpuAt([double[]]$secs) {
    $t = ''
    foreach ($s in $secs) { $t += ("2026/11/01 08:00:{0}, 1500, 1500, 6000, 50.00, 80.00, 60, 0x0000000000000000`n" -f $s.ToString('00.000', $inv)) }
    ConvertFrom-ClocksPowerGpuCsv $t $cst
}
function CpuAt([double[]]$secs) {
    $t = "`"(PDH-CSV 4.0) (Central Standard Time)(360)`",`"\\B\Processor Information(_Total)\% Processor Performance`",`"\\B\Processor Information(_Total)\Processor Frequency`"`n"
    foreach ($s in $secs) { $t += ("`"11/01/2026 08:00:{0}`",`"100.0`",`"2000.0`"`n" -f $s.ToString('00.000', $inv)) }
    ConvertFrom-ClocksPowerCpuCsv $t $cst
}
function Chk($interval, $net, $width) { $c = [ordered]@{ intervalMs = $interval; netStepMs = $net }; if ($null -ne $width) { $c['captureWidthMs'] = $width }; $c }
$win = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 1, 14, 0, 3, 0, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 1, 14, 0, 50, 0, [DateTimeKind]::Utc); clockWhy = $null }
$plat = [ordered]@{ powerPlan = 'Balanced'; acState = 'ac'; display = 'x' }
# a GPU window the witness refuses: a row every 1.0 s with ONE 1.55 s gap (the venue's measured shape, 0 of 51 samples)
$gpuSecs = @(); $t0 = 0.1; for ($k = 0; $k -lt 51; $k++) { $gpuSecs += $t0; $t0 += $(if ($k -eq 25) { 1.55 } else { 1.0 }) }
$gpuBad = T { Get-ClocksPowerGpuSummary (GpuAt $gpuSecs) $running $win $null (Chk 1000 0 5) }
$gpuOk = T { Get-ClocksPowerGpuSummary (GpuAt @(10, 11, 12, 13, 14)) $running $win $null (Chk 1000 0 5) }
$cpuOk = T { Get-ClocksPowerCpuSummary (CpuAt @(10, 11, 12, 13, 14)) $running $win $null (Chk 1000 0 5) }
$cpuNoWin = T { Get-ClocksPowerCpuSummary (CpuAt @(10, 11, 12, 13, 14)) $running $null }
$cpuDead = T { Get-ClocksPowerCpuSummary $null ([ordered]@{ status = 'unavailable'; reason = 'typeperf-missing' }) $win }
$r['gpuBadStatus'] = $gpuBad.status
$r['cpuOkStatus'] = $cpuOk.status
$r['cpuDeadStatus'] = $cpuDead.status
$r['cpuNoWinStatus'] = $cpuNoWin.status
$b1 = T { Get-ClocksPowerBlock $gpuBad $cpuOk $plat $win $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['refusedGpuCpuOk'] = T { @{ status = $b1.status; reason = $b1.reason; gpuStatus = $b1.gpu.status; advisory = $b1.gpu.advisory; why = [string]$b1.gpu.advisoryReason; gaps = $b1.gpu.rowGapMs; raw = $b1.gpu.rawFile; samples = $b1.gpu.samples; total = $b1.gpu.samplesTotal; cpuAdvisory = ($b1.cpu.Contains('advisory')) } }
$b2 = T { Get-ClocksPowerBlock $gpuOk $cpuDead $plat $win $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['gpuOkCpuDead'] = T { @{ status = $b2.status; reason = [string]$b2.reason; advisory = $b2.gpu.advisory } }
$b3 = T { Get-ClocksPowerBlock $null $cpuOk $plat $null $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['gpuNullCpuOkNoWindow'] = T { @{ status = $b3.status; reason = [string]$b3.reason; advisory = $b3.gpu.advisory } }
$b4 = T { Get-ClocksPowerBlock $gpuBad $cpuNoWin $plat $win $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['cpuPartial'] = T { @{ status = $b4.status; reason = [string]$b4.reason } }
$b5 = T { Get-ClocksPowerBlock $gpuOk $cpuOk $plat $win $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['bothOk'] = T { @{ status = $b5.status; reason = $b5.reason; advisory = $b5.gpu.advisory } }
$b6 = T { Get-ClocksPowerBlock $null $null $null $null $false 1000 'gpu-clocks.csv' 'cpu-clocks.csv' }
$r['noneAtAll'] = T { @{ status = $b6.status; reason = [string]$b6.reason; advisory = $b6.gpu.advisory } }
# adversary ade3c7d5 A1: the GPU sampler's executable comes from two fixed local paths, never from a PATH walk (a slow share on PATH cost one
# synchronous Test-Path per entry before any telemetry deadline was checked)
$nvRoot = Join-Path $env:CP_TMP 'nv'
$nvSys = Join-Path $nvRoot 'sysroot'; $nvPf = Join-Path $nvRoot 'pf'; $nvPath = Join-Path $nvRoot 'pathdir'
foreach ($d in @((Join-Path $nvSys 'System32'), (Join-Path $nvPf 'NVIDIA Corporation\NVSMI'), $nvPath)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
$nvSysExe = Join-Path $nvSys 'System32\nvidia-smi.exe'; $nvPfExe = Join-Path $nvPf 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'
[IO.File]::WriteAllText((Join-Path $nvPath 'nvidia-smi.exe'), 'x')
$oldSys = $env:SystemRoot; $oldPf = $env:ProgramFiles; $oldPath = $env:PATH
try {
    $env:SystemRoot = $nvSys; $env:ProgramFiles = $nvPf; $env:PATH = $nvPath
    $r['nvOnlyOnPath'] = T { Resolve-ClocksPowerNvidiaSmi }
    [IO.File]::WriteAllText($nvPfExe, 'x')
    $r['nvNvsmi'] = T { (Resolve-ClocksPowerNvidiaSmi) -ceq $nvPfExe }
    [IO.File]::WriteAllText($nvSysExe, 'x')
    $r['nvSystem32First'] = T { (Resolve-ClocksPowerNvidiaSmi) -ceq $nvSysExe }
    $env:SystemRoot = '\\slow-share-a\win'; $env:ProgramFiles = '\\slow-share-b\pf'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r['nvUnc'] = T { Resolve-ClocksPowerNvidiaSmi }
    $r['nvUncFast'] = ($sw.ElapsedMilliseconds -lt 2000)
    $env:SystemRoot = ''; $env:ProgramFiles = ''
    $r['nvBlank'] = T { Resolve-ClocksPowerNvidiaSmi }
} finally { $env:SystemRoot = $oldSys; $env:ProgramFiles = $oldPf; $env:PATH = $oldPath }
J $r
"""


@requires_windows_pwsh
class ClocksPowerRoundEightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="clocks-power-r8-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER_R8, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None
        cls.text = lf(GENERATOR.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_r8_the_fixture_shapes_are_what_the_tests_say_they_are(self) -> None:
        self.assertEqual(self.r["gpuBadStatus"], "unavailable(sample-clock-unverified)", "one 1.55 s gap among 1.0 s gaps: the venue's refusal shape")
        self.assertEqual(self.r["cpuOkStatus"], "ok")
        self.assertTrue(self.r["cpuDeadStatus"].startswith("unavailable("), self.r["cpuDeadStatus"])
        self.assertEqual(self.r["cpuNoWinStatus"], "partial(no_window)")

    def test_r8_a_refused_gpu_witness_never_sets_the_status_or_the_reason(self) -> None:
        b = self.r["refusedGpuCpuOk"]
        self.assertEqual((b["status"], b["reason"]), ("ok", None), "the CPU half is usable with a window: the block is ok, the GPU refusal is not a gate")
        self.assertEqual(b["gpuStatus"], "unavailable(sample-clock-unverified)", "the GPU summary still says what happened")
        self.assertEqual(b["samples"], 0)

    def test_r8_the_gpu_block_is_marked_advisory_with_its_reason_and_row_gaps(self) -> None:
        b = self.r["refusedGpuCpuOk"]
        self.assertIs(b["advisory"], True)
        self.assertIn("BENCH-GPU-CLOCKS-FLUSH-1", b["why"])
        self.assertIn("never sets clocksPowerStatus", b["why"])
        self.assertEqual(b["gaps"]["n"], 50, "the rowGapMs disclosure survives the descope")
        self.assertEqual(b["gaps"]["maxMs"], 1550.0)
        self.assertEqual(b["raw"], "gpu-clocks.csv", "the raw GPU file stays published: it is the evidence the follow-up card needs")
        self.assertFalse(b["cpuAdvisory"], "only the GPU half is advisory")
        for key in ("gpuOkCpuDead", "gpuNullCpuOkNoWindow", "bothOk", "noneAtAll"):
            self.assertIs(self.r[key]["advisory"], True, key + ": the flag is on every GPU block, whatever its status")

    def test_r8_the_status_reflects_the_cpu_half_only(self) -> None:
        self.assertEqual(self.r["gpuOkCpuDead"]["status"], "unavailable", "a healthy GPU does not rescue a dead CPU half")
        self.assertTrue(self.r["gpuOkCpuDead"]["reason"].startswith("cpu=unavailable("), self.r["gpuOkCpuDead"])
        self.assertNotIn("gpu=", self.r["gpuOkCpuDead"]["reason"])
        self.assertEqual(self.r["gpuNullCpuOkNoWindow"], {"status": "partial", "reason": "window=no_window", "advisory": True})
        self.assertEqual(self.r["cpuPartial"], {"status": "partial", "reason": "cpu=partial(no_window)"})
        self.assertEqual(self.r["bothOk"], {"status": "ok", "reason": None, "advisory": True})
        n = self.r["noneAtAll"]
        self.assertEqual(n["status"], "unavailable")
        self.assertNotIn("gpu=", n["reason"])
        self.assertIn("cpu=", n["reason"])

    def test_r8_a1_the_gpu_sampler_finds_nvidia_smi_in_two_local_paths_and_never_walks_path(self) -> None:
        self.assertEqual(self.r["nvOnlyOnPath"], "", "an nvidia-smi on PATH only is not found: PATH is never walked")
        self.assertIs(self.r["nvNvsmi"], True, "the NVSMI folder is the second candidate")
        self.assertIs(self.r["nvSystem32First"], True, "System32 wins")
        self.assertEqual(self.r["nvUnc"], "", "a UNC root is skipped, not probed")
        self.assertIs(self.r["nvUncFast"], True)
        self.assertEqual(self.r["nvBlank"], "", "a blank root is skipped")

    def test_r8_a1_the_clocks_call_site_uses_the_local_resolver_and_the_job_carries_it(self) -> None:
        sites = [ln for ln in self.text.splitlines() if "Start-ClocksPowerGpu $clocksPowerBackend" in ln]
        self.assertEqual(len(sites), 1)
        self.assertIn("(Resolve-ClocksPowerNvidiaSmi)", sites[0])
        self.assertNotIn("Resolve-UtilSampleNvidiaSmi", sites[0], "the util sampler's resolver walks PATH")
        self.assertEqual(self.text.count("${function:Resolve-ClocksPowerNvidiaSmi}"), 1, "the job carries the helper")
        fn = re.search(r"(?ms)^function Resolve-ClocksPowerNvidiaSmi\b.*?^}", self.text)
        self.assertIsNotNone(fn)
        self.assertNotIn("env:PATH", fn.group(0))

    def test_r8_the_block_function_never_reads_the_gpu_status(self) -> None:
        block = re.search(r"(?ms)^function Get-ClocksPowerBlock\b.*?^}", self.text)
        self.assertIsNotNone(block)
        body = block.group(0)
        self.assertNotIn("$Gpu.status", body, "the GPU summary's status is copied into the block as a disclosure and read nowhere")
        self.assertNotIn("gpuUsable", body)


@requires_windows_pwsh
class ClocksPowerRunnerMappingTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        from tools.repo_hygiene.test_util_sample_leg import UTIL_STUB_GENERATOR
        stub = UTIL_STUB_GENERATOR.replace("[switch]$UtilSample", "[switch]$UtilSample, [switch]$ClocksPower, [int]$ClocksPowerIntervalMs = 0, [switch]$ClocksPowerWarmup", 1)
        self.assertIn("[switch]$ClocksPower", stub, "the stub generator's param list moved")
        self.make_harness()
        self.gen.write_text(stub, encoding="utf-8")

    def spec_with(self, args: dict | None, leg_id: str) -> Path:
        path = self.write_spec(card="BENCH-CLOCKS-POWER-LOG-1", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        if args is not None:
            spec["generatorArgs"] = args
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def fields(self, args: dict | None, leg_id: str) -> dict[str, str]:
        if self.gen_log.exists():
            self.gen_log.unlink()
        proc, _receipt, _submitted = self.run_leg("bachelor", self.spec_with(args, leg_id))
        calls = self.generator_calls()
        self.assertTrue(calls, proc.stdout[-2000:] + proc.stderr[-2000:])
        return dict(kv.split("=", 1) for kv in calls[-1].split(";") if "=" in kv)

    def test_c6_only_a_json_true_passes_clocks_power(self) -> None:
        for name, args, want in (("true", {"clocksPower": True}, "True"), ("false", {"clocksPower": False}, None), ("absent", {}, None), ("none", None, None)):
            with self.subTest(case=name):
                self.assertEqual(self.fields(args, f"cp-{name}").get("ClocksPower"), want)

    def test_c6_the_interval_and_the_warm_up_mark_pass_only_with_the_sampler(self) -> None:
        got = self.fields({"clocksPower": True, "clocksPowerIntervalMs": 2000, "clocksPowerWarmup": True}, "cp-opts")
        self.assertEqual((got.get("ClocksPower"), got.get("ClocksPowerIntervalMs"), got.get("ClocksPowerWarmup")), ("True", "2000", "True"))
        got = self.fields({"clocksPowerIntervalMs": 2000, "clocksPowerWarmup": True}, "cp-opts-off")
        self.assertIsNone(got.get("ClocksPower"))
        self.assertIsNone(got.get("ClocksPowerIntervalMs"), "an interval with no sampler is nothing")

    def test_c6_a_string_clocks_power_is_refused_before_the_generator_runs(self) -> None:
        self.gen_log.write_text("", encoding="utf-8")
        proc, _receipt, _submitted = self.run_leg("bachelor", self.spec_with({"clocksPower": "true"}, "cp-string"))
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("DVE_LEG_SPEC_INVALID generatorArgs.clocksPower must be JSON true or false", out)
        self.assertEqual(self.generator_calls(), [])


class ClocksPowerSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def test_c7_the_fields_are_typed_and_bounded(self) -> None:
        import jsonschema
        props = self.schema["properties"]["generatorArgs"]["properties"]
        self.assertEqual(props["clocksPower"]["type"], "boolean")
        self.assertIn("Absent or false", props["clocksPower"]["description"])
        self.assertEqual(props["clocksPowerWarmup"]["type"], "boolean")
        self.assertEqual((props["clocksPowerIntervalMs"]["type"], props["clocksPowerIntervalMs"]["minimum"], props["clocksPowerIntervalMs"]["maximum"]), ("integer", 250, 5000))
        base = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-windowed-s1.json").read_text(encoding="utf-8"))
        good = json.loads(json.dumps(base))
        good["generatorArgs"].update({"clocksPower": True, "clocksPowerIntervalMs": 1000, "clocksPowerWarmup": False})
        jsonschema.validate(good, self.schema)
        for bad_args in ({"clocksPower": "true"}, {"clocksPowerIntervalMs": 100}, {"clocksPowerIntervalMs": 5001}, {"clocksPowerIntervalMs": "1000"}, {"clocksPowerWarmup": 1}):
            bad = json.loads(json.dumps(base))
            bad["generatorArgs"].update(bad_args)
            with self.assertRaises(jsonschema.ValidationError, msg=str(bad_args)):
                jsonschema.validate(bad, self.schema)

    def test_c7_the_runner_guard_list_names_the_new_booleans(self) -> None:
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        m = re.search(r"\$script:GeneratorArgsBooleanSwitches = @\(([^)]*)\)", text)
        self.assertIsNotNone(m)
        names = re.findall(r"'([A-Za-z]+)'", m.group(1))
        for name in ("clocksPower", "clocksPowerWarmup"):
            self.assertIn(name, names)
            self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"][name]["type"], "boolean")


if __name__ == "__main__":
    unittest.main()
