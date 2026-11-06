"""Tests for BENCH-START-CAP-FROM-LEG-COUNT-1: a venue run's START CAP is sized from its planned leg count and leg duration, not from a hand-typed window.

WHY. The lane-local venue drivers (bench-ab.ps1, never committed) took a mandatory, hand-typed -StartCapUtc and refused to start another batch past it. BLUR r3
(PLAYBACK-CUDA-FULLSCREEN-BLUR-UPLOAD-STALL-POST-502-1) was given a 29 min window for a plan whose batches take ~34 min each, and PRESENT-TAIL #720 stopped at 8 of 12
cells (START_CAP 12:30Z, stopReason=START_CAP legsLaunched=32). The tracked runner had the same shape: Invoke-VenueLeaseBatch.ps1 defaulted its deadline to a fixed
6 h whatever the plan size.

WHAT IS PINNED (tools/profiling/dual-venue/VenueChain.psm1 Get-VenueRunStartCap, Get-VenueLegPlaySeconds; Get-VenueStartCap.ps1; Invoke-VenueLeaseBatch.ps1)
  * RED on fork/master (the helper does not exist there): a 12-leg plan at the measured 235 s per leg (playSeconds 25 + 210 s overhead, PRESENT-TAIL r1b batch-01..03
    gaps 225-236 s) cannot finish inside the fixed 38 min / 29 min windows that cut those runs, and the computed cap covers the last leg's end plus a margin;
  * the cap grows with the leg count and with the leg duration (and the lease cooldown / budget waits are inside the projection);
  * an explicit caller cap is kept exactly (source=explicit), even when it is shorter than the computed one or later than the hard bound;
  * a computed cap past the existing hard bound is clamped to it and says so on the typed line START_CAP_CLAMPED_TO_HARD_BOUND;
  * the per-leg seconds come from the leg spec (playSeconds), and the CLI prints the START_CAP line and `StartCapUtc=<iso>`;
  * Invoke-VenueLeaseBatch.ps1 sizes its default deadline from the plan, never below the legacy 6 h, and keeps an explicit -DeadlineUtc.

Offline only: pure functions plus the CLI. Nothing reaches a venue. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
CHAIN = DV / "VenueChain.psm1"
CLI = DV / "Get-VenueStartCap.ps1"
BATCH = DV / "Invoke-VenueLeaseBatch.ps1"
LEG_SPEC = DV / "legs" / "m16-1243-display-windowed-s1.json"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

NOW = "2026-10-30T09:20:16Z"
LEG_WALL = 235            # playSeconds 25 + the measured 210 s per-leg overhead
LIVE_CAPS = "-CapSec 2700 -CooldownSec 300 -BudgetSec 9720 -WindowSec 10800"   # Bachelor's live caps, as the CAPS line of the cut runs read them


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def call(args: str, now: str = NOW, extra_setup: str = "") -> dict:
    """Run Get-VenueRunStartCap with -NowUtc and return the result as a dict (times as Z text)."""
    script = (
        f"Import-Module '{CHAIN}' -Force -DisableNameChecking; {extra_setup} "
        f"$r = Get-VenueRunStartCap -NowUtc '{now}' {args}; "
        "$o = [ordered]@{ capUtc = $r.capUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'); source = $r.source; "
        "computedCapUtc = $(if ($null -ne $r.computedCapUtc) { $r.computedCapUtc.ToString('yyyy-MM-ddTHH:mm:ssZ') } else { $null }); projectedEndUtc = $r.projectedEndUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'); "
        "spanSec = $r.spanSec; batches = $r.batches; legWallSec = $r.legWallSec; waitSec = $r.waitSec; marginSec = $r.marginSec; clamped = $r.clamped; "
        "projectedPastBound = $r.projectedPastBound; lines = @($r.lines) }; $o | ConvertTo-Json -Compress"
    )
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError(f"pwsh failed rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")
    return json.loads(p.stdout.strip().splitlines()[-1])


@requires_windows_pwsh
class StartCapFromLegCountTests(unittest.TestCase):
    def test_twelve_leg_plan_is_not_cut_before_leg_12_by_the_fixed_windows(self):
        # RED on fork/master: Get-VenueRunStartCap does not exist there. The semantics it pins are the incident's: under the 38 min (BLUR r3 successor text) and
        # 29 min (r3 window) fixed caps, 12 legs at 235 s each cannot all start; the computed cap reaches past the last leg's end.
        r = call(f"-LegCount 12 {LIVE_CAPS}")
        start = parse(NOW)
        twelve_legs_sec = 12 * LEG_WALL
        for fixed_window_min in (29, 38):
            self.assertLess(fixed_window_min * 60, twelve_legs_sec, "the fixed window is shorter than the 12 legs: it would cut before leg 12")
        self.assertEqual(r["legWallSec"], LEG_WALL)
        self.assertGreaterEqual((parse(r["projectedEndUtc"]) - start).total_seconds(), twelve_legs_sec)
        self.assertGreater(parse(r["capUtc"]), parse(r["projectedEndUtc"]), "the cap carries a margin past the last leg")
        self.assertEqual(r["source"], "computed")
        self.assertTrue(r["lines"][0].startswith("START_CAP source=computed "), r["lines"][0])
        # 12 legs of 235 s do not fit one 2700 s lease: the second lease waits out the 300 s cooldown, and that wait is inside the projection
        self.assertEqual(r["batches"], 2)
        self.assertGreaterEqual(r["waitSec"], 300)

    def test_cap_grows_with_leg_count_and_leg_duration(self):
        small = call(f"-LegCount 4 {LIVE_CAPS}")
        big = call(f"-LegCount 24 {LIVE_CAPS}")
        self.assertGreater(parse(big["capUtc"]), parse(small["capUtc"]))
        short_legs = call(f"-LegCount 12 -PlaySeconds 25 {LIVE_CAPS}")
        long_legs = call(f"-LegCount 12 -PlaySeconds 120 {LIVE_CAPS}")
        self.assertGreater(parse(long_legs["capUtc"]), parse(short_legs["capUtc"]))
        self.assertEqual(long_legs["legWallSec"], 120 + 210)

    def test_lease_budget_wait_from_earlier_leases_is_inside_the_projection(self):
        # PRESENT-TAIL r1b: B04 sat behind an 83 min BUDGET wait. Earlier leases that fill the 3 h budget push the whole run later; the cap must follow.
        n = parse(NOW)
        leases = ", ".join(
            f"[pscustomobject]@{{ startUtc = '{(n - timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%SZ')}'; endUtc = '{(n - timedelta(minutes=m - 45)).strftime('%Y-%m-%dT%H:%M:%SZ')}' }}"
            for m in (165, 110, 60)
        )
        tight = "-CapSec 2700 -CooldownSec 300 -BudgetSec 5400 -WindowSec 10800"   # the old mirrored budget: two earlier 45 min leases in the window leave no room
        free = call(f"-LegCount 12 {tight}")
        busy = call(f"-LegCount 12 {tight} -Leases @({leases})")
        self.assertGreater(busy["waitSec"], free["waitSec"])
        self.assertGreater(parse(busy["capUtc"]), parse(free["capUtc"]))

    def test_explicit_caller_cap_is_kept(self):
        short = "2026-10-30T09:30:00Z"   # far shorter than 12 legs need
        r = call(f"-LegCount 12 {LIVE_CAPS} -ExplicitCapUtc '{short}'")
        self.assertEqual(r["capUtc"], short)
        self.assertEqual(r["source"], "explicit")
        self.assertTrue(r["lines"][0].startswith("START_CAP source=explicit "), r["lines"][0])
        self.assertGreater(parse(r["computedCapUtc"]), parse(short), "the computed value is reported beside it, never substituted")
        # an explicit cap later than the hard bound is the caller's call and is kept too
        late = "2026-10-30T23:00:00Z"
        r2 = call(f"-LegCount 12 {LIVE_CAPS} -ExplicitCapUtc '{late}' -HardBoundUtc '2026-10-30T10:00:00Z'")
        self.assertEqual(r2["capUtc"], late)
        self.assertEqual(r2["source"], "explicit")

    def test_computed_cap_is_clamped_to_the_hard_bound_with_the_typed_line(self):
        bound = "2026-10-30T10:00:00Z"   # 40 min after NOW: 12 legs (2 leases + cooldown) end at ~10:13Z
        r = call(f"-LegCount 12 {LIVE_CAPS} -HardBoundUtc '{bound}'")
        self.assertEqual(r["capUtc"], bound)
        self.assertEqual(r["source"], "clamped")
        self.assertTrue(r["clamped"])
        self.assertTrue(r["projectedPastBound"], "the legs do not all start before the bound: said, not hidden")
        typed = [ln for ln in r["lines"] if ln.startswith("START_CAP_CLAMPED_TO_HARD_BOUND ")]
        self.assertEqual(len(typed), 1, r["lines"])
        self.assertIn(f"hardBoundUtc={bound}", typed[0])
        self.assertIn("projectedPastBound=1", typed[0])
        self.assertLessEqual(parse(r["capUtc"]), parse(bound))

    def test_a_bound_beyond_the_computed_cap_leaves_it_alone(self):
        r = call(f"-LegCount 12 {LIVE_CAPS} -HardBoundUtc '2026-10-31T09:00:00Z'")
        self.assertEqual(r["source"], "computed")
        self.assertFalse(r["clamped"])
        self.assertEqual(r["capUtc"], r["computedCapUtc"])
        self.assertFalse([ln for ln in r["lines"] if ln.startswith("START_CAP_CLAMPED")])

    def test_leg_play_seconds_come_from_the_leg_spec(self):
        script = (
            f"Import-Module '{CHAIN}' -Force -DisableNameChecking; "
            f"Write-Output (Get-VenueLegPlaySeconds -SpecPath '{LEG_SPEC}'); "
            "Write-Output ([string]::IsNullOrEmpty([string](Get-VenueLegPlaySeconds -SpecPath 'C:\\no\\such\\spec.json')))"
        )
        p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.split()
        self.assertEqual(out[0], str(json.loads(LEG_SPEC.read_text(encoding="utf-8"))["playSeconds"]))
        self.assertEqual(out[1], "True")

    def test_cli_prints_the_typed_line_and_the_cap(self):
        p = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegCount", "12", "-NowUtc", NOW, "-CooldownSec", "300", "-BudgetSec", "9720",
             "-HardEndUtc", "2026-10-30T10:00:00Z"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("START_CAP source=clamped "), lines[0])
        self.assertTrue(any(ln.startswith("START_CAP_CLAMPED_TO_HARD_BOUND ") for ln in lines))
        self.assertEqual(lines[-1], "StartCapUtc=2026-10-30T10:00:00Z")

    def test_cli_derives_legs_and_seconds_from_leg_specs(self):
        specs = [str(LEG_SPEC)] * 12
        p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegSpec", ",".join(specs), "-NowUtc", NOW, "-CooldownSec", "300", "-BudgetSec", "9720"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        first = p.stdout.strip().splitlines()[0]
        self.assertIn("legs=12 ", first)
        self.assertIn(f"playSec={json.loads(LEG_SPEC.read_text(encoding='utf-8'))['playSeconds']} ", first)

    def test_cli_without_a_plan_is_a_usage_error(self):
        p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI)], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 2)


def run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)


@requires_windows_pwsh
class StartCapAdversaryTests(unittest.TestCase):
    """The adversary pass on 9c80cfab (A1-A6): each finding's input, fed to the helper, the CLI or the runner's pin."""

    def test_a1_the_runners_own_intra_lease_cooldown_is_inside_the_projection(self):
        # the runner sleeps -CooldownSec after every completed leg while work remains (Invoke-VenueLeaseBatch.ps1): 12 legs at 3600 s are >= 11 x 3600 s of sleeping
        plain = call(f"-LegCount 12 {LIVE_CAPS}")
        r = call(f"-LegCount 12 -LegCooldownSec 600 {LIVE_CAPS}")
        self.assertGreater(r["spanSec"], plain["spanSec"] + 11 * 600 - 1200, "11 sleeps of 600 s (less what the lease cap absorbs) lengthen the span")
        self.assertGreater(parse(r["capUtc"]), parse(plain["capUtc"]))
        self.assertEqual(r["legWallSec"], LEG_WALL + 600)
        self.assertIn("legCooldownSec=600", r["lines"][0])
        # the sleep after a leg is not part of the room a leg needs to START: 3600 s sleeps give one leg per lease, not a refusal
        big = call(f"-LegCount 12 -LegCooldownSec 3600 {LIVE_CAPS}")
        self.assertGreaterEqual(big["batches"], 12)
        self.assertGreaterEqual(big["spanSec"], 11 * 2700)
        src = BATCH.read_text(encoding="utf-8")
        self.assertIn("-LegCooldownSec $CooldownSec", src, "the runner hands its own -CooldownSec to the projection")

    def test_a2_a_lease_that_holds_no_leg_is_unsizable_never_forced_to_progress(self):
        p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 1 -CapSec 60")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("START_CAP_UNSIZABLE", p.stderr + p.stdout)
        # the same through the CLI: a typed line and exit 3, no StartCapUtc
        c = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegCount", "1", "-NowUtc", NOW, "-CapSec", "60"], capture_output=True, text=True, timeout=120)
        self.assertEqual(c.returncode, 3, c.stdout + c.stderr)
        self.assertIn("START_CAP_UNSIZABLE", c.stdout)
        self.assertNotIn("StartCapUtc=", c.stdout)
        # a budget with no room is the same refusal (zero or negative room)
        p2 = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 1 -BudgetSec 0")
        self.assertNotEqual(p2.returncode, 0)
        self.assertIn("START_CAP_UNSIZABLE", p2.stderr + p2.stdout)
        # a leg longer than the lease: one leg's cost is the floor of the leg budget, so a 3000 s leg in a 2700 s lease is unsizable too
        p3 = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 1 -PlaySeconds 2800")
        self.assertNotEqual(p3.returncode, 0)
        self.assertIn("START_CAP_UNSIZABLE", p3.stderr + p3.stdout)

    def test_a2_an_explicit_cap_is_not_stopped_by_an_unsizable_projection(self):
        r = call(f"-LegCount 1 -CapSec 60 -ExplicitCapUtc '2026-10-30T09:40:00Z'")
        self.assertEqual(r["capUtc"], "2026-10-30T09:40:00Z")
        self.assertEqual(r["source"], "explicit")
        self.assertIn("projection=UNSIZABLE", r["lines"][0])
        self.assertIsNone(r["computedCapUtc"])

    def test_a3_a_numeric_string_play_seconds_counts_like_the_runner_casts_it(self):
        with tempfile.TemporaryDirectory() as d:
            spec = json.loads(LEG_SPEC.read_text(encoding="utf-8"))
            spec["playSeconds"] = "120"
            s = Path(d) / "str-seconds.json"
            s.write_text(json.dumps(spec), encoding="utf-8")
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Write-Output (Get-VenueLegPlaySeconds -SpecPath '{s}')")
            self.assertEqual(p.stdout.strip(), "120", p.stderr)

    def test_a3_an_unreadable_spec_counts_at_the_longest_readable_leg_and_is_said(self):
        with tempfile.TemporaryDirectory() as d:
            spec = json.loads(LEG_SPEC.read_text(encoding="utf-8"))
            spec["playSeconds"] = 120
            good = Path(d) / "good.json"
            good.write_text(json.dumps(spec), encoding="utf-8")
            missing = Path(d) / "missing.json"
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; $r = Get-VenuePlanPlaySeconds -SpecPaths @('{good}','{missing}'); "
                       "Write-Output \"$($r.playSeconds)|$($r.unknown)|$($r.known)|$($r.line)\"")
            self.assertEqual(p.returncode, 0, p.stderr)
            secs, unknown, known, line = p.stdout.strip().split("|", 3)
            self.assertEqual((secs, unknown, known), ("120", "1", "1"))
            self.assertTrue(line.startswith("START_CAP_PLAYSECONDS_UNKNOWN "), line)
            # no readable spec at all: the default stands, and it is said
            p2 = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; $r = Get-VenuePlanPlaySeconds -SpecPaths @('{missing}'); Write-Output \"$($r.playSeconds)|$($r.line)\"")
            self.assertTrue(p2.stdout.strip().startswith("25|START_CAP_PLAYSECONDS_UNKNOWN "), p2.stdout + p2.stderr)
        src = BATCH.read_text(encoding="utf-8")
        self.assertIn("Get-VenuePlanPlaySeconds", src)
        self.assertIn("Get-VenuePlanPlaySeconds", CLI.read_text(encoding="utf-8"))

    def test_a4_huge_lease_settings_never_throw_an_int32_conversion(self):
        for extra in ("-CooldownSec 2147483647", "-CooldownSec 1000000000 -MarginPct 500"):
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; $r = Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 12 {extra}; Write-Output $r.line")
            self.assertEqual(p.returncode, 0, f"{extra}: {p.stderr}")
            self.assertTrue(p.stdout.startswith("START_CAP source=computed "), p.stdout)
        # a projection that leaves the DateTime range is START_CAP_UNSIZABLE, and an explicit cap still stands
        p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 10000 -CooldownSec 2147483647")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("START_CAP_UNSIZABLE", p.stderr + p.stdout)
        r = call("-LegCount 10000 -CooldownSec 2147483647 -ExplicitCapUtc '2026-10-30T12:00:00Z'")
        self.assertEqual((r["capUtc"], r["source"]), ("2026-10-30T12:00:00Z", "explicit"))
        # the runner catches a sizing failure and keeps the legacy window
        src = BATCH.read_text(encoding="utf-8")
        self.assertRegex(src, r'\} catch \{ L "START_CAP source=legacy ')

    def test_a5_a_leg_spec_path_with_a_comma_is_one_leg(self):
        with tempfile.TemporaryDirectory() as d:
            spec = json.loads(LEG_SPEC.read_text(encoding="utf-8"))
            spec["playSeconds"] = 1200
            s = Path(d) / "a,b.json"
            s.write_text(json.dumps(spec), encoding="utf-8")
            c = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegSpec", str(s), "-NowUtc", NOW], capture_output=True, text=True, timeout=120)
            self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
            self.assertIn("legs=1 playSec=1200 ", c.stdout.splitlines()[0])

    def test_a6_projected_past_bound_means_the_last_leg_starts_after_the_bound(self):
        # one leg starts at +40 s; a bound at +200 s is not passed by its START (its end, +275 s, is)
        r = call(f"-LegCount 1 -HardBoundUtc '2026-10-30T09:23:36Z'")
        self.assertTrue(r["clamped"])
        self.assertFalse(r["projectedPastBound"])
        self.assertIn("projectedPastBound=0", r["lines"][1])


@requires_windows_pwsh
class StartCapAdversaryPass2Tests(unittest.TestCase):
    """The adversary pass on 59aaa91b (A1-A5): each finding's input."""

    def test_p2a1_a_sleep_longer_than_the_lease_is_not_cut_short_by_the_lease(self):
        # the runner sleeps the full -CooldownSec after a leg whatever the lease has left: with 3600 s sleeps the twelfth leg starts at 22:06:21Z at the earliest
        r = call(f"-LegCount 12 -LegCooldownSec 3600 {LIVE_CAPS}")
        self.assertGreaterEqual(parse(r["capUtc"]), parse("2026-10-30T22:06:21Z"), r["lines"][0])
        self.assertGreaterEqual(parse(r["projectedEndUtc"]), parse("2026-10-30T22:06:21Z"))

    def test_p2a2_the_leg_budget_follows_the_longest_planned_leg(self):
        # the runner's leg budget grows to the longest leg seen: one 2000 s leg among 99 of 25 s makes every later lease need 2210 s of room to start a leg
        r = call(f"-LegCount 100 -PlaySeconds 45 -MaxPlaySeconds 2000 {LIVE_CAPS}")
        self.assertGreaterEqual(r["batches"], 50)
        self.assertGreaterEqual(parse(r["capUtc"]), parse("2026-10-30T21:04:56Z"), "the arithmetic replay's last start is the cap's floor")
        with tempfile.TemporaryDirectory() as d:
            long_spec = json.loads(LEG_SPEC.read_text(encoding="utf-8")); long_spec["playSeconds"] = 2000
            short_spec = json.loads(LEG_SPEC.read_text(encoding="utf-8")); short_spec["playSeconds"] = 25
            a = Path(d) / "long.json"; a.write_text(json.dumps(long_spec), encoding="utf-8")
            b = Path(d) / "short.json"; b.write_text(json.dumps(short_spec), encoding="utf-8")
            specs = ",".join([str(a)] + [str(b)] * 99)
            c = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegSpec", specs, "-NowUtc", NOW, "-CooldownSec", "300", "-BudgetSec", "9720"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
            cap = [ln for ln in c.stdout.splitlines() if ln.startswith("StartCapUtc=")][0].split("=", 1)[1]
            self.assertGreaterEqual(parse(cap), parse("2026-10-30T21:04:56Z"))

    def test_p2a3_play_seconds_strings_read_as_the_runner_casts_them(self):
        with tempfile.TemporaryDirectory() as d:
            for text in ("0x4b0", "1,200", "1200"):
                spec = json.loads(LEG_SPEC.read_text(encoding="utf-8")); spec["playSeconds"] = text
                s = Path(d) / "s.json"; s.write_text(json.dumps(spec), encoding="utf-8")
                p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Write-Output (Get-VenueLegPlaySeconds -SpecPath '{s}')")
                self.assertEqual(p.stdout.strip(), "1200", f"{text!r}: {p.stderr}")

    def test_p2a4_an_explicit_cap_keeps_its_sub_second_digits(self):
        c = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), "-LegCount", "1", "-NowUtc", NOW, "-ExplicitCapUtc", "2026-10-30T10:00:00.900Z"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertEqual(c.stdout.strip().splitlines()[-1], "StartCapUtc=2026-10-30T10:00:00.9Z")
        self.assertIn("capUtc=2026-10-30T10:00:00.9Z", c.stdout.splitlines()[0])

    def test_p2a5_an_array_root_spec_is_not_a_leg_spec(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "array.json"; s.write_text(json.dumps([{"playSeconds": 120}]), encoding="utf-8")
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Write-Output ([string]::IsNullOrEmpty([string](Get-VenueLegPlaySeconds -SpecPath '{s}')))")
            self.assertEqual(p.stdout.strip(), "True", p.stderr)


def runner_admission(plays, now, cap=2700, cooldown=86400, overhead=210, margin=120, openclose=40, leg_budget_floor=360):
    """Independent replay of the batch runner's admission arithmetic (Invoke-VenueLeaseBatch.ps1: a leg starts only while the lease has legBudget + leaseMargin of room;
    the leg budget grows to the longest leg of the plan, up to 2700 s). Valid where the 3 h lease budget does not bind (cooldown >= window), so a lease opens one cooldown
    after the previous one ends. Returns (batches, last_leg_start)."""
    runs = [p + overhead for p in plays]
    budget = min(2700, max(leg_budget_floor, max(runs)))
    t, i, batches, last_start = now, 0, 0, now
    while i < len(plays):
        open_at = t if batches == 0 else t + timedelta(seconds=cooldown)
        s, first = openclose, True
        while i < len(plays):
            if not first and s > cap - margin - budget:
                break
            last_start = open_at + timedelta(seconds=s)
            s += runs[i]
            i += 1
            first = False
        t = open_at + timedelta(seconds=s)
        batches += 1
    return batches, last_start


def write_spec(d: Path, name: str, play) -> str:
    spec = json.loads(LEG_SPEC.read_text(encoding="utf-8"))
    spec["playSeconds"] = play
    p = d / name
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(CLI), *args], capture_output=True, text=True, timeout=180)


def cli_cap(p: subprocess.CompletedProcess) -> str:
    return [ln for ln in p.stdout.splitlines() if ln.startswith("StartCapUtc=")][0].split("=", 1)[1]


@requires_windows_pwsh
class StartCapAdversaryR2Tests(unittest.TestCase):
    """The adversary pass on 26531966 (r1 pass 3: A1-A4), BENCH-START-CAP-FROM-LEG-COUNT-1 r2. Each test feeds the finding's input."""

    def test_r2a1_the_stride_follows_each_legs_own_duration_not_the_plan_mean(self):
        # 63 legs of 1290 s then 37 of 25 s: the mean (822 s) strides the long legs at 1032 s, so the helper projected 50 batches where the runner's admission needs 71
        with tempfile.TemporaryDirectory() as d:
            long_spec = write_spec(Path(d), "long.json", 1290)
            short_spec = write_spec(Path(d), "short.json", 25)
            plays = [1290] * 63 + [25] * 37
            c = cli("-LegSpec", ",".join([long_spec] * 63 + [short_spec] * 37), "-NowUtc", NOW, "-CapSec", "2700", "-CooldownSec", "86400", "-WindowSec", "10800", "-BudgetSec", "5400")
            self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
            first = c.stdout.splitlines()[0]
            batches = int(re.search(r" batches=(\d+) ", first).group(1))
            want_batches, want_last_start = runner_admission(plays, parse(NOW))
            self.assertEqual(want_batches, 71, "the replay reproduces the adversary's runner figure")
            self.assertGreaterEqual(batches, want_batches, first)
            self.assertGreaterEqual(parse(cli_cap(c)), want_last_start, first)

    def test_r2a1_equal_legs_project_exactly_as_the_scalar_plan_does(self):
        # the per-leg list changes nothing when every leg is the same length
        with tempfile.TemporaryDirectory() as d:
            spec = write_spec(Path(d), "s25.json", 25)
            a = cli("-LegSpec", ",".join([spec] * 12), "-NowUtc", NOW, "-CooldownSec", "300", "-BudgetSec", "9720")
            b = cli("-LegCount", "12", "-PlaySeconds", "25", "-NowUtc", NOW, "-CooldownSec", "300", "-BudgetSec", "9720")
            self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
            self.assertEqual(cli_cap(a), cli_cap(b))
            self.assertEqual(re.search(r" batches=(\d+) ", a.stdout.splitlines()[0]).group(1), re.search(r" batches=(\d+) ", b.stdout.splitlines()[0]).group(1))

    def test_r2a1_the_runner_hands_the_per_leg_list_to_the_projection(self):
        src = BATCH.read_text(encoding="utf-8")
        self.assertIn("-LegPlaySeconds $capPlan.legSeconds", src)
        cli_src = CLI.read_text(encoding="utf-8")
        self.assertIn("$legSeconds = @($planSecs.legSeconds)", cli_src)
        self.assertIn("$args2.LegPlaySeconds = $legSeconds", cli_src)

    def test_r2a1_a_bad_per_leg_value_or_count_is_a_typed_refusal(self):
        for extra in ("-LegCount 2 -LegPlaySeconds @(25, 36001)", "-LegCount 2 -LegPlaySeconds @(25, 0)", "-LegCount 2 -LegPlaySeconds @(25, [double]::NaN)", "-LegCount 3 -LegPlaySeconds @(25, 30)"):
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' {extra}")
            self.assertNotEqual(p.returncode, 0, extra)
            self.assertIn("START_CAP_", p.stderr + p.stdout, extra)

    def test_r2a2_an_unparseable_hard_end_is_a_typed_refusal_never_an_unclamped_cap(self):
        c = cli("-LegCount", "1", "-NowUtc", NOW, "-HardEndUtc", "not-a-time")
        self.assertNotEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertIn("START_CAP_BAD_TIME", c.stdout)
        self.assertIn("HardEndUtc", c.stdout)
        self.assertNotIn("StartCapUtc=", c.stdout)

    def test_r2a2_every_other_time_input_is_refused_the_same_way(self):
        for flag in ("-ExplicitCapUtc", "-NowUtc"):
            args = ["-LegCount", "1", "-NowUtc", NOW] if flag != "-NowUtc" else ["-LegCount", "1"]
            c = cli(*args, flag, "garbage")
            self.assertNotEqual(c.returncode, 0, f"{flag}: {c.stdout}{c.stderr}")
            self.assertIn("START_CAP_BAD_TIME", c.stdout, flag)
            self.assertNotIn("StartCapUtc=", c.stdout, flag)
        # a bad bound is refused even when a valid explicit cap would have been kept
        c = cli("-LegCount", "1", "-NowUtc", NOW, "-HardEndUtc", "not-a-time", "-ExplicitCapUtc", "2026-10-30T10:00:00Z")
        self.assertNotEqual(c.returncode, 0)
        self.assertIn("START_CAP_BAD_TIME", c.stdout)
        # the helper itself throws, so the runner's sizing catch keeps the legacy window and says why
        p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Get-VenueRunStartCap -NowUtc '{NOW}' -LegCount 1 -HardBoundUtc 'not-a-time'")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("START_CAP_BAD_TIME", p.stderr + p.stdout)

    def test_r2a3_a_zoneless_now_is_utc_in_the_cli_exactly_as_in_the_helper(self):
        # the CLI read a zoneless -NowUtc as HOST-LOCAL time (a Central host: 6 h later) while the helper read it as UTC. One parser: zoneless = UTC, and an explicit offset
        # (the non-UTC offsets below do not depend on the host zone) is honoured by both
        cases = {
            "2026-11-01T01:30:00": "2026-11-01T01:30:00Z",
            "2026-11-01T01:30:00Z": "2026-11-01T01:30:00Z",
            "2026-11-01T01:30:00-06:00": "2026-11-01T07:30:00Z",
            "2026-11-01T01:30:00+05:30": "2026-10-31T20:00:00Z",
        }
        for given, as_utc in cases.items():
            c = cli("-LegCount", "1", "-NowUtc", given)
            self.assertEqual(c.returncode, 0, f"{given}: {c.stdout}{c.stderr}")
            h = call("-LegCount 1", now=as_utc)
            self.assertEqual(cli_cap(c), h["capUtc"], f"-NowUtc {given!r} must read as {as_utc} in the CLI as in the helper")

    def test_r2a3_the_cli_has_no_parser_of_its_own(self):
        src = CLI.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("DateTimeOffset]::Parse", code)
        self.assertNotIn("DateTime]::Parse", code)
        self.assertIn("ConvertTo-VenueStartCapTime", code)
        self.assertRegex(src, r"zoneless[^\n]*UTC", "the help text says a zoneless time is UTC")

    def test_r2a4_a_play_seconds_just_over_int32_is_unreadable_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            huge = write_spec(Path(d), "huge.json", 2147483647.1)
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; Write-Output ([string]::IsNullOrEmpty([string](Get-VenueLegPlaySeconds -SpecPath '{huge}')))")
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "True"), p.stderr)
            self.assertEqual(p.stderr.strip(), "")
            # the CLI keeps a supplied explicit cap for that spec
            c = cli("-LegSpec", huge, "-NowUtc", NOW, "-ExplicitCapUtc", "2026-10-30T10:00:00Z")
            self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
            self.assertEqual(cli_cap(c), "2026-10-30T10:00:00Z")
            self.assertIn("START_CAP_PLAYSECONDS_UNKNOWN", c.stdout)

    def test_r2a4_the_whole_number_range_is_checked_in_double_before_any_int_cast(self):
        with tempfile.TemporaryDirectory() as d:
            want = {"36000": 36000, "35999.2": 36000, "36000.5": None, "1e300": None, "-5": None, "0": None, "0.4": 1, "99999999999999999999": None,
                    "2147483647": None, "2147483648": None, "2147483647.1": None, "2147483646.5": None, "1200": 1200}
            specs = {k: write_spec(Path(d), f"s{i}.json", json.loads(k)) for i, k in enumerate(want)}
            lines = "; ".join(f"Write-Output ('{k}=' + [string](Get-VenueLegPlaySeconds -SpecPath '{p}'))" for k, p in specs.items())
            p = run_ps(f"Import-Module '{CHAIN}' -Force -DisableNameChecking; {lines}")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stderr.strip(), "")
            got = dict(ln.split("=", 1) for ln in p.stdout.strip().splitlines())
            for k, v in want.items():
                self.assertEqual(got[k], "" if v is None else str(v), f"playSeconds {k}")

    def test_r2a4_the_cli_reads_specs_inside_its_try_catch(self):
        src = CLI.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        self.assertLess(code.index("try {"), code.index("Get-VenuePlanPlaySeconds"), "the spec read sits inside the try")
        self.assertLess(code.index("Get-VenuePlanPlaySeconds"), code.index("catch {"))


class LeaseBatchWiringTests(unittest.TestCase):
    """Source pins on the tracked runner: the default deadline is sized from the plan, an explicit one is kept."""

    @classmethod
    def setUpClass(cls):
        cls.src = BATCH.read_text(encoding="utf-8")

    def test_default_deadline_is_sized_from_the_plan_and_never_below_the_legacy_window(self):
        self.assertIn("$t0.AddHours(6)", self.src, "the legacy 6 h window stays as the floor")
        m = re.search(r"if \(\$DeadlineUtc\) \{\s*\n\s*L \"START_CAP source=explicit.*?\n\} else \{(.*?)\n\}\nL \"BATCH_RUN_START", self.src, re.S)
        self.assertIsNotNone(m, "the START_CAP block sits right before BATCH_RUN_START")
        body = m.group(1)
        self.assertIn("Get-VenueRunStartCap", body)
        self.assertIn("-LegCount ([Math]::Max(1, $plan.Count))", body)
        self.assertIn("Get-VenuePlanPlaySeconds", body, "the per-leg seconds come from the leg specs")
        for caps in ("-CapSec $LeaseCapSec", "-CooldownSec $LeaseCooldownSec", "-BudgetSec $LeaseBudgetSec", "-Leases @($leases)"):
            self.assertIn(caps, body, f"the run's resolved lease caps feed the projection: {caps}")
        self.assertIn("if ($startCap.capUtc -gt $deadline)", body, "the computed cap only ever extends the legacy window")

    def test_explicit_deadline_is_logged_and_kept(self):
        self.assertRegex(self.src, r'if \(\$DeadlineUtc\) \{\s*\n\s*L "START_CAP source=explicit capUtc=')

    def test_module_exports_and_the_cli_exist(self):
        chain = CHAIN.read_text(encoding="utf-8")
        self.assertIn("Get-VenueRunStartCap, Get-VenueLegPlaySeconds, Get-VenuePlanPlaySeconds", chain[chain.index("Export-ModuleMember"):])
        self.assertTrue(CLI.is_file())


if __name__ == "__main__":
    unittest.main()
