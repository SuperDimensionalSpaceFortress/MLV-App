"""HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11): no MLVApp GUI or playback run of any kind on the hub VM.

The hub VM (Virtual-Ten) is a VMware guest on Ultra-Magnus: a local playback shows a window on the owner's machine and contaminates
the Ultra-Magnus venue legs. Incident: producer prompt PLAYBACK-CPU-PACE-GAP-1 r1 said to run the exactness arms "where a GUI
desktop session and the clip exist"; the lane ran the app's smoke entry on the hub.

One refusal per surface, each with a RED fixture here:
  * the app entries (platform/qt/HubHostPlaybackGuard.h, main.cpp): pinned to tools/profiling/dual-venue/hub-hosts.json and wired
    in tests/console/test_hub_host_playback_guard.cpp (the C++ side needs a GUI build; this file pins the copies of the host list);
  * the tracked launcher scripts: an inline guard, executed here with COMPUTERNAME=VIRTUAL-TEN;
  * the lane wrapper tools/coordination/Invoke-Lane.ps1: a lane on a hub host whose prompt tells it to run playback locally is
    refused before any process or run dir exists;
  * the prompt lint tools/coordination/lane-local-playback-lint.ps1 (also run by .claude-state/hub-autopilot/dispatch-perf-lane.ps1).

Nothing here launches MLVApp or a lane. The two executed surfaces (Invoke-Lane, the launchers) are driven only down their REFUSAL path.
Launch command lines in the fixtures are assembled at run time so this file itself never contains one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOSTS_JSON = ROOT / "tools" / "profiling" / "dual-venue" / "hub-hosts.json"
VENUES_JSON = ROOT / "tools" / "profiling" / "dual-venue" / "venues.json"
GUARD_HEADER = ROOT / "platform" / "qt" / "HubHostPlaybackGuard.h"
LINT_PS1 = ROOT / "tools" / "coordination" / "lane-local-playback-lint.ps1"
INVOKE_LANE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
PROFILING = ROOT / "tools" / "profiling"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

# Pieces of launch command lines, assembled so no literal command line sits in this file.
APP = "MLVApp" + ".exe"
SMOKE_MODE = "--gui-smoke-" + "playback"
SMOKE_SCRIPT = "run-release-" + "gui-smoke.ps1"

# The tracked launchers that start the app in a playback / smoke / profile mode on THIS machine. Each carries the inline guard.
GUARDED_LAUNCHERS = [
    "capture-reference-frame.ps1",
    "run-local-gpu-capability.ps1",
    SMOKE_SCRIPT,
    "run-release-playback-profile.ps1",
    "start-release-cuda-playback.ps1",
    "test-app-play-gate-offscreen.ps1",
    "validate-visible-playback.ps1",
]
# Scripts that name a playback flag but run it on a venue (over ssh), so the hub-host guard has nothing to guard.
REMOTE_ONLY = {
    "run-ultra-magnus-profile.ps1",
    "ssh-gpu-probe.ps1",
}
GUARD_MARKER = "HUB-VM-NO-LOCAL-PLAYBACK-1 guard"

# The incident wording, as the producer prompt carried it.
INCIDENT_LINE = ("  - Run it where a GUI desktop session and the M16-1243 clip exist. "
                 "If the clip is not on this host, run the two arms as venue jobs through the chain runner.")


def _config() -> dict:
    return json.loads(HOSTS_JSON.read_text(encoding="utf-8"))


def _code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


class HubHostsFileTests(unittest.TestCase):
    def test_file_names_the_hub_and_no_venue(self):
        cfg = _config()
        self.assertEqual("mlv-app/hub-hosts/v1", cfg["schema"])
        self.assertIn("VIRTUAL-TEN", cfg["hubHosts"])
        venue_hosts = {v["expectedHost"].upper() for v in json.loads(VENUES_JSON.read_text(encoding="utf-8"))["venues"].values()}
        self.assertTrue(venue_hosts, "venues.json lists no expectedHost")
        self.assertEqual(set(), {h.upper() for h in cfg["hubHosts"]} & venue_hosts, "a venue can never be a hub host")
        self.assertEqual("HUB_VM_LOCAL_PLAYBACK_REFUSED", cfg["refusalToken"])
        self.assertNotEqual(0, cfg["refusalExitCode"])
        self.assertEqual("CI_FIXTURE_ONLY", cfg["ciFixtureOverrideValue"])


class CopiesArePinnedToTheFileTests(unittest.TestCase):
    """The app and the launchers cannot read the JSON at run time, so each carries a copy; a copy that drifts fails here."""

    def test_cpp_header_constants_equal_the_file(self):
        cfg = _config()
        text = GUARD_HEADER.read_text(encoding="utf-8")
        hosts_block = re.search(r"kHubHosts\[\]\s*=\s*\{(.*?)\}", text, re.DOTALL)
        self.assertIsNotNone(hosts_block, "kHubHosts[] not found")
        self.assertEqual(cfg["hubHosts"], re.findall(r'"([^"]+)"', hosts_block.group(1)))
        for const, key in (("kRefusalToken", "refusalToken"), ("kOverrideEnv", "ciFixtureOverrideEnv"), ("kOverrideValue", "ciFixtureOverrideValue")):
            found = re.search(const + r'\s*=\s*"([^"]+)"', text)
            self.assertIsNotNone(found, const)
            self.assertEqual(cfg[key], found.group(1), const)
        code = re.search(r"kRefusalExitCode\s*=\s*(\d+)", text)
        self.assertIsNotNone(code)
        self.assertEqual(cfg["refusalExitCode"], int(code.group(1)))

    def test_every_launcher_guard_equals_the_file(self):
        cfg = _config()
        for name in GUARDED_LAUNCHERS:
            with self.subTest(launcher=name):
                text = (PROFILING / name).read_text(encoding="utf-8")
                self.assertIn(GUARD_MARKER, text, f"{name} lost its hub-host guard")
                hosts = re.search(r"\$hubHostsNoLocalPlayback\s*=\s*@\((.*?)\)", text)
                self.assertIsNotNone(hosts, name)
                self.assertEqual(cfg["hubHosts"], re.findall(r"'([^']+)'", hosts.group(1)), name)
                self.assertIn(f"$env:{cfg['ciFixtureOverrideEnv']} -cne '{cfg['ciFixtureOverrideValue']}'", text, name)
                self.assertRegex(text, rf"exit {cfg['refusalExitCode']}\b", name)
                self.assertIn(cfg["refusalToken"], text, name)

    def test_guard_is_the_first_statement_after_the_param_block(self):
        for name in GUARDED_LAUNCHERS:
            with self.subTest(launcher=name):
                lines = (PROFILING / name).read_text(encoding="utf-8").splitlines()
                marker = next(i for i, ln in enumerate(lines) if GUARD_MARKER in ln)
                before = lines[:marker]
                close = max(i for i, ln in enumerate(before) if ln.strip() == ")")
                between = [ln for ln in before[close + 1 :] if ln.strip() and not ln.lstrip().startswith("#")]
                self.assertEqual([], between, f"{name}: nothing executable may sit between the param( ) block and the guard")


class LauncherCensusTests(unittest.TestCase):
    """A new script that starts the app in a playback / smoke / profile mode must carry the guard (or be named remote-only here)."""

    def test_every_tracked_launcher_is_guarded_or_remote(self):
        mode = re.compile(r"--(gui-smoke-playback|profile-playback)\b|MLVAPP_AUTOPLAY_SECONDS\s*=\s*['\"]?[1-9]")
        unguarded = []
        for script in sorted((ROOT / "tools").rglob("*.ps1")):
            if script.name in REMOTE_ONLY or script.name in GUARDED_LAUNCHERS:
                continue
            code = "\n".join(_code_lines(script))
            if mode.search(code) and GUARD_MARKER not in code and "HubHostPlaybackGuard" not in code:
                unguarded.append(str(script.relative_to(ROOT)))
        self.assertEqual([], unguarded, "scripts that start an MLVApp playback mode with no hub-host guard")

    def test_remote_only_scripts_really_run_on_a_venue(self):
        for name in REMOTE_ONLY:
            with self.subTest(script=name):
                text = (PROFILING / name).read_text(encoding="utf-8")
                self.assertRegex(text, r"(?i)ssh|um-run|Invoke-Command|ultra-?magnus|um-ssh", name)


class VenueChainOnlyTests(unittest.TestCase):
    """The job generator is run by the venue chain (Invoke-VenueLeg / Invoke-VenueChain) and by nothing else that executes it."""

    GENERATOR = "playback-attr-3-cuda-job.ps1"
    CHAIN = {
        "tools/profiling/dual-venue/Invoke-VenueLeg.ps1",
        "tools/profiling/dual-venue/Invoke-VenueChain.ps1",
        "tools/profiling/dual-venue/VenueChain.psm1",
        "tools/profiling/dual-venue/DualVenueRunner.psm1",
        "tools/profiling/dual-venue/New-VenueFlavorPair.ps1",
        "tools/profiling/dual-venue/New-VenueSheetPair.ps1",
    }

    def test_nothing_outside_the_chain_executes_the_generator(self):
        call = re.compile(r"(?:^|[\s;(])(?:&|\.)\s+['\"]?[^\n]*" + re.escape(self.GENERATOR) + r"|-File\s+['\"]?\S*" + re.escape(self.GENERATOR))
        offenders = []
        for script in sorted((ROOT / "tools").rglob("*.ps*1")):
            rel = script.relative_to(ROOT).as_posix()
            if rel in self.CHAIN or script.name == self.GENERATOR:
                continue
            if any(call.search(ln) for ln in _code_lines(script)):
                offenders.append(rel)
        self.assertEqual([], offenders)

    def test_the_chain_reaches_the_generator_by_its_own_path(self):
        self.assertIn(self.GENERATOR, (PROFILING / "dual-venue" / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8"))

    def test_the_job_a_generator_emits_launches_through_a_guarded_launcher(self):
        # The emitted job is what plays; it does so only through the smoke launcher, which refuses on a hub host.
        gen = (PROFILING / "bachelor" / self.GENERATOR).read_text(encoding="utf-8")
        self.assertIn(SMOKE_SCRIPT, gen)
        self.assertIn(SMOKE_SCRIPT, GUARDED_LAUNCHERS)


@requires_pwsh
class PromptLintTests(unittest.TestCase):
    """Executes the real lint in pwsh against RED (the incident and its relatives) and GREEN (prompts that must pass) fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hubvm-lint-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _findings(self, text: str) -> list[str]:
        src = self.tmp / "in.txt"
        src.write_text(text, encoding="utf-8")
        probe = self.tmp / "probe.ps1"
        probe.write_text(
            ". '" + str(LINT_PS1) + "'\n"
            "$f = @(Get-LocalPlaybackPromptFindings -Text (Get-Content -LiteralPath '" + str(src) + "' -Raw))\n"
            "ConvertTo-Json -InputObject @($f) -Compress\n",
            encoding="ascii",
        )
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout.strip() or "[]")

    def test_red_the_incident_line(self):
        found = self._findings("## VENUE\n" + INCIDENT_LINE + "\n")
        self.assertTrue(any(f.startswith("where-a-gui-session-exists") for f in found), found)

    def test_red_relatives_of_the_incident(self):
        for text in (
            f"Run the exactness arms locally with {APP} and compare.",
            "Launch the playback smoke locally to check the dump.",
            f"Run MLVApp {SMOKE_MODE.lstrip('-')} on this hub VM and attach the log.",
            f"Then run `{APP} {SMOKE_MODE} --input clip.mlv --seconds 25` and read the result.",
            f"Run: pwsh -NoProfile -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu",
            "Run it where a desktop session and the clip exist.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_green_prompts_that_must_pass(self):
        for text in (
            "",
            "## NO LOCAL PLAYBACK (owner rule 2026-10-11, binding)\nNever launch MLVApp (any playback, gui-smoke-playback, playback-smoke) on this hub VM (Virtual-Ten). "
            "This rule overrides any line above that says to run playback where a GUI session and the clip exist.\n\n## NEXT\nBuild and run the console tests.\n",
            "Run the two arms as venue jobs through the chain runner on Bachelor.",
            "HOST RULE: NO MLVApp GUI or playback run of any kind on this hub VM (Virtual-Ten), not performance and not exactness.",
            "Never run playback locally; every playback run goes to a venue.",
            "The incident prompt said to run the exactness arms \"where a GUI desktop session and the clip exist\"; that wording is now refused.",
            "Run C1DumpDllParityVectors locally with MLVAPP_C1_VECTORS_DIR at the base commit and HEAD.",
            "Build and run the console tests: console_tests.exe --gtest_filter=HubHostPlaybackGuard.*",
            f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor.",
            f"Grep for {APP} / --seconds / --autoplay / --profile-playback in every launcher and apply the same oracle.",
            f"- tools/profiling/{SMOKE_SCRIPT}:2821: comment only.",
        ):
            with self.subTest(text=text[:70]):
                self.assertEqual([], self._findings(text), text)

    def test_the_binding_block_is_removed_only_up_to_the_next_heading(self):
        text = f"## NO LOCAL PLAYBACK\nNever do it.\n## STEPS\nRun the arms locally with {APP}.\n"
        self.assertTrue(self._findings(text), "a real instruction AFTER the binding block must still be caught")

    def test_hub_host_decision(self):
        probe = self.tmp / "host.ps1"
        probe.write_text(
            ". '" + str(LINT_PS1) + "'\n"
            "$o = [ordered]@{}\n"
            "$o.hub = Test-HubHost -ComputerName 'VIRTUAL-TEN'\n"
            "$o.hubLower = Test-HubHost -ComputerName 'virtual-ten'\n"
            "$o.bachelor = Test-HubHost -ComputerName 'BACHELOR'\n"
            "$o.um = Test-HubHost -ComputerName 'ULTRA-MAGNUS'\n"
            "$o.partial = Test-HubHost -ComputerName 'VIRTUAL-TEN-2'\n"
            "$o.empty = Test-HubHost -ComputerName ''\n"
            "$env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE = '1'\n"
            "$o.truthy = Test-HubHost -ComputerName 'VIRTUAL-TEN'\n"
            "$env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE = 'CI_FIXTURE_ONLY'\n"
            "$o.override = Test-HubHost -ComputerName 'VIRTUAL-TEN'\n"
            "$o | ConvertTo-Json -Compress\n",
            encoding="ascii",
        )
        env = {k: v for k, v in os.environ.items() if k != "MLVAPP_HUB_PLAYBACK_CI_FIXTURE"}
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(
            {"hub": True, "hubLower": True, "bachelor": False, "um": False, "partial": False, "empty": False, "truthy": True, "override": False},
            json.loads(done.stdout.strip()),
        )


@requires_pwsh
class InvokeLaneRefusalTests(unittest.TestCase):
    """A lane on the hub host whose prompt asks for local playback is refused before any process, run dir or receipt exists."""

    def _run(self, host: str, prompt: str, run_dir: Path):
        env = dict(os.environ, COMPUTERNAME=host)
        env.pop("MLVAPP_HUB_PLAYBACK_CI_FIXTURE", None)
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(INVOKE_LANE), "-Lane", "sonnet", "-Prompt", prompt,
             "-WorkDir", str(ROOT), "-RunDir", str(run_dir), "-TimeoutSec", "60"],
            capture_output=True, text=True, timeout=180, env=env,
        )

    def test_red_hub_host_with_the_incident_prompt_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory(prefix="hubvm-lane-") as tmp:
            run_dir = Path(tmp) / "run"
            done = self._run("VIRTUAL-TEN", "Design.\n" + INCIDENT_LINE + "\n", run_dir)
            self.assertNotEqual(0, done.returncode)
            out = done.stdout + done.stderr
            self.assertIn("local-playback-prompt-refused", out)
            self.assertIn("HUB_VM_LOCAL_PLAYBACK_REFUSED", out)
            self.assertFalse(run_dir.exists(), "the refusal must come before any run dir exists")

    def test_the_gate_sits_before_every_process_launch_and_run_dir_creation(self):
        text = INVOKE_LANE.read_text(encoding="utf-8")
        gate = text.index("local-playback-prompt-refused")
        # Call sites only (a function DEFINITION that mentions New-Item sits earlier in the file and runs nothing).
        for later in ("Write-Utf8NoBom $promptPath", "$hookCopyPath = ", "# ---------------------------------------------------------------- 0.1 pre-flight"):
            at = text.find(later)
            if at != -1:
                self.assertLess(gate, at, f"gate must precede {later!r}")


@requires_pwsh
class LauncherGuardBlockTests(unittest.TestCase):
    """Runs ONLY the guard block of each tracked launcher (never the launcher): the hub host is refused with the typed exit code,
    a venue host and the exact CI-fixture override fall through. Running the launcher itself with no clip is exactly the act the rule forbids."""

    @staticmethod
    def _guard_block(name: str) -> str:
        lines = (PROFILING / name).read_text(encoding="utf-8").splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("$hubHostsNoLocalPlayback"))
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == "}")
        return "\n".join(lines[start : end + 1])

    def _run_block(self, name: str, host: str, override: str | None):
        cfg = _config()
        with tempfile.TemporaryDirectory(prefix="hubvm-guard-") as tmp:
            probe = Path(tmp) / "guard.ps1"
            probe.write_text(self._guard_block(name) + "\nWrite-Output 'GUARD-PASSED'\n", encoding="ascii")
            env = dict(os.environ, COMPUTERNAME=host)
            env.pop(cfg["ciFixtureOverrideEnv"], None)
            if override is not None:
                env[cfg["ciFixtureOverrideEnv"]] = override
            return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)],
                                  capture_output=True, text=True, timeout=120, env=env)

    def test_each_launcher_guard_refuses_the_hub_host(self):
        cfg = _config()
        for name in GUARDED_LAUNCHERS:
            with self.subTest(launcher=name):
                done = self._run_block(name, "VIRTUAL-TEN", None)
                out = done.stdout + done.stderr
                self.assertEqual(cfg["refusalExitCode"], done.returncode, out)
                self.assertIn(cfg["refusalToken"], out)
                self.assertNotIn("GUARD-PASSED", out)
                self.assertEqual(cfg["refusalExitCode"], self._run_block(name, "virtual-ten", "1").returncode, "a truthy value is not the override")

    def test_each_launcher_guard_lets_a_venue_and_the_fixture_override_through(self):
        cfg = _config()
        for name in GUARDED_LAUNCHERS:
            with self.subTest(launcher=name):
                for host, override in (("BACHELOR", None), ("ULTRA-MAGNUS", None), ("VIRTUAL-TEN", cfg["ciFixtureOverrideValue"])):
                    done = self._run_block(name, host, override)
                    self.assertEqual(0, done.returncode, f"{host}/{override}: {done.stdout}{done.stderr}")
                    self.assertIn("GUARD-PASSED", done.stdout)


if __name__ == "__main__":
    unittest.main()
