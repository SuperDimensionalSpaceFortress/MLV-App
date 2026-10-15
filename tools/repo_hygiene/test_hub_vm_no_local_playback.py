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

import itertools
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


LINT_REL = "tools/coordination/lane-local-playback-lint.ps1"
# Where the differential pin takes the baseline lint from: the first ref that resolves (HUB_VM_LINT_BASELINE_REF overrides).
BASELINE_REFS = ("fork/master", "origin/master", "master")


def _config() -> dict:
    return json.loads(HOSTS_JSON.read_text(encoding="utf-8"))


def _baseline_lint_source() -> tuple[str, str] | None:
    """(ref, text) of the lint at the first baseline ref that resolves, or None when no ref does (e.g. a shallow CI clone)."""
    refs = ((os.environ["HUB_VM_LINT_BASELINE_REF"],) if os.environ.get("HUB_VM_LINT_BASELINE_REF") else BASELINE_REFS)
    for ref in refs:
        done = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{LINT_REL}"], capture_output=True, timeout=60)
        if done.returncode == 0 and done.stdout:
            return ref, done.stdout.decode("utf-8")
    return None


def _lint_finding_counts(lint: Path, texts: list[str], work: Path, tag: str) -> "subprocess.Popen":
    """Start ONE pwsh that dot-sources `lint` and counts the findings of every text (one sentence per line). Returns the Popen."""
    src = work / f"{tag}-in.txt"
    src.write_text("\n".join(texts) + "\n", encoding="utf-8")
    probe = work / f"{tag}-probe.ps1"
    probe.write_text(
        ". '" + str(lint) + "'\n"
        "$out = New-Object System.Collections.Generic.List[int]\n"
        "foreach ($l in [System.IO.File]::ReadAllLines('" + str(src) + "')) { $out.Add(@(Get-LocalPlaybackPromptFindings -Text $l).Count) }\n"
        "ConvertTo-Json -InputObject @($out.ToArray()) -Compress\n",
        encoding="ascii",
    )
    return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _collect_counts(proc: "subprocess.Popen", expected: int) -> list[int]:
    out, err = proc.communicate(timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"lint probe failed: {err}")
    counts = json.loads(out.strip())
    if len(counts) != expected:
        raise AssertionError(f"lint probe returned {len(counts)} verdicts for {expected} sentences")
    return counts


def _differential_corpus(seeds: tuple[str, ...]) -> dict[str, dict]:
    """text -> the fields it was built from: launch verb x playback word x local/host/here marker x venue word x negator / contrast word x joiner."""
    rows: dict[str, dict] = {}

    def add(text: str, **fields) -> None:
        rows.setdefault(text, fields)

    def join(head: str, word: str, tail: str) -> str:
        return f"{head}{word} {tail}" if word.startswith(",") else f"{head} {word} {tail}"

    verbs = ("Run", "Launch")
    pbs = ("playback", "the arms")
    markers = ("locally", "on this host", "here")
    venues = ("on Bachelor", "through the venue chain")
    nouns = ("Bachelor", "the venue chain")
    contrasts = ("not", ", not", "without", "instead of", "rather than", "nor", ", nor", ", never")
    joiners = (" and", ",", " then", " but")
    for v, pb, m in itertools.product(verbs, pbs, markers):
        add(f"{v} {pb} {m}.", tpl="bare", M=m)
        for ven in venues:
            add(f"{v} {pb} {m} {ven}.", tpl="marker-then-venue", M=m, V=ven)
            add(f"{v} {pb} {ven} {m}.", tpl="venue-then-marker", M=m, V=ven)
            for c in contrasts:
                add(join(f"{v} {pb} {m}", c, ven) + ".", tpl="marker-contrast-venue", M=m, V=ven, C=c)
                add(join(f"{v} {pb} {ven}", c, m) + ".", tpl="venue-contrast-marker", M=m, V=ven, C=c)
                for neg in ("Do not", "Never"):
                    add(join(f"{neg} {v.lower()} {pb} {m}", c, ven) + ".", tpl="neg-marker-contrast-venue", M=m, V=ven, C=c, N=neg)
            for j in joiners:
                add(f"{v} {pb} {m}{j} {ven}.", tpl="marker-join-venue", M=m, V=ven, J=j)
                add(f"{v} {pb} {ven}{j} {m}.", tpl="venue-join-marker", M=m, V=ven, J=j)
        for m2 in markers:
            if m2 == m:
                continue
            for c in contrasts:
                add(join(f"{v} {pb} {m}", c, m2) + ".", tpl="marker-contrast-marker", M=m, M2=m2, C=c)
        for ven in venues:
            for verb in ("copy", "read"):
                add(f"{v} {pb} {ven} and {verb} the log {m}.", tpl="handling-tail", M=m, V=ven)
        for noun in nouns:
            for c0 in ("Without", "Instead of", "Rather than", "Skip", "Bypass", "Not"):
                add(f"{c0} {noun}, {v.lower()} {pb} {m}.", tpl="contrast-first", M=m, C=c0, V=noun)
    for text in seeds:
        add(text, tpl="seed")
    return rows


def _differential_flips(work: Path) -> tuple[str, list[tuple[str, dict]], list[tuple[str, dict]], int]:
    """Run baseline and candidate lints over the corpus. -> (baseline ref, refuse->pass rows, pass->refuse rows, corpus size)."""
    base = _baseline_lint_source()
    if base is None:
        raise unittest.SkipTest("no baseline ref resolves (fork/master, origin/master, master); the differential pin needs git history")
    ref, text = base
    baseline = work / "baseline-lint.ps1"
    baseline.write_text(text, encoding="utf-8")
    corpus = _differential_corpus(PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_PASS + PromptLintTests.PROBE_ROWS)
    texts = list(corpus)
    procs = (_lint_finding_counts(baseline, texts, work, "base"), _lint_finding_counts(LINT_PS1, texts, work, "cand"))
    before = _collect_counts(procs[0], len(texts))
    after = _collect_counts(procs[1], len(texts))
    opened = [(t, corpus[t]) for t, b, a in zip(texts, before, after) if b > 0 and a == 0]
    closed = [(t, corpus[t]) for t, b, a in zip(texts, before, after) if b == 0 and a > 0]
    return ref, opened, closed, len(texts)


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

    def test_all_three_layers_compare_the_override_exactly_and_untrimmed(self):
        # The launchers compare -cne with no Trim (pinned above); the header and the lint must open on the same set.
        header = GUARD_HEADER.read_text(encoding="utf-8")
        body = re.search(r"inline bool overrideActive\([^)]*\)\s*\{(.*?)\}", header, re.DOTALL)
        self.assertIsNotNone(body, "overrideActive() not found")
        self.assertNotIn("trimmed", body.group(1))
        lint_line = next(ln for ln in _code_lines(LINT_PS1) if "$overrideNow" in ln and "ciFixtureOverrideValue" in ln)
        self.assertIn("-ceq", lint_line)
        self.assertNotIn("Trim", lint_line)

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

    # HUB-VM-LINT-CLAUSE-SCOPE-1 (Sol review of PR #401): a negation or a venue word exempts only the clause it sits in. The first
    # five rows are the review's probe sentences verbatim; the rest are the same defect with the clauses joined by a comma / "and".
    CLAUSE_MUST_REFUSE = (
        "Run playback here.",
        "Play the clip locally in MLVApp.",
        "Run playback on this host after the venue job finishes.",
        "Do not run the venue chain. Run playback locally.",
        "Do not run the venue chain, run playback locally.",
        "Do not run the venue chain and run playback locally.",
        "Do not use the venue chain, run playback here.",
        "Run the arms on Bachelor and on this host.",
        "Do not wait for Bachelor, play the clip locally in MLVApp.",
        # r2 (CoS review of PR #448, r1b head 6702288e): a NEGATED venue word is not a routing. The first five were refused on master and
        # passed on r1b; the last is a soft pre-existing under-refusal (the "without" negates the venue chain, not the launch).
        "Run playback locally without the venue chain.",
        "Run playback locally, not on Bachelor.",
        "Run playback locally instead of on Bachelor.",
        "Run the arms locally rather than on Bachelor.",
        "Launch MLVApp locally instead of using the venue chain.",
        "Run playback on this host without the venue chain.",
    )
    CLAUSE_MUST_PASS = (
        "Run playback locally on Bachelor through the venue chain.",
        "Run playback on Bachelor through the venue chain, not on this host.",
        "Run playback through the venue chain and read the log here.",
        "Never run playback locally, nor on this host.",
        "Never run playback locally and on this host.",
        "Run the arms on Bachelor through the venue chain and never locally.",
        "Run playback on Bachelor rather than locally.",
    )

    # Rows that are in no catalogue tuple above but belong in the differential corpus as seeds.
    PROBE_ROWS = (
        "Run playback through the venue chain and copy the log locally.",
    )

    def test_red_clause_scope_equivalent_wordings_are_refused(self):
        for text in self.CLAUSE_MUST_REFUSE:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_green_clause_scope_venue_routed_wordings_pass(self):
        for text in self.CLAUSE_MUST_PASS:
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)

    # Soft, pre-existing over-refusal (refused on master and on r1b): `locally` sits in a clause that copies a log, not in the clause that
    # launches playback. Exempting it needs a handling-verb list, and a list opens "Copy the clip locally and run playback." (the incident
    # shape), so r2 records it as a hardening row. Marked expectedFailure: fixing it flips this to an unexpected success and the suite
    # then asks for the decorator to be removed and the row moved into CLAUSE_MUST_PASS.
    @unittest.expectedFailure
    def test_hardening_a_log_copied_locally_after_a_venue_run_is_not_a_launch(self):
        self.assertEqual([], self._findings("Run playback through the venue chain and copy the log locally."))

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
            "$env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE = ' CI_FIXTURE_ONLY '\n"
            "$o.padded = Test-HubHost -ComputerName 'VIRTUAL-TEN'\n"
            "$env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE = 'CI_FIXTURE_ONLY'\n"
            "$o.override = Test-HubHost -ComputerName 'VIRTUAL-TEN'\n"
            "$o | ConvertTo-Json -Compress\n",
            encoding="ascii",
        )
        env = {k: v for k, v in os.environ.items() if k != "MLVAPP_HUB_PLAYBACK_CI_FIXTURE"}
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(
            {"hub": True, "hubLower": True, "bachelor": False, "um": False, "partial": False, "empty": False, "truthy": True, "padded": True, "override": False},
            json.loads(done.stdout.strip()),
        )


@requires_pwsh
class LintDifferentialPinTests(unittest.TestCase):
    """HUB-VM-LINT-CLAUSE-SCOPE-1 r2 (CoS prevention note). A catalogue only proves the rows somebody thought of; r1b added an exemption
    (a venue word read before `locally`) that silently opened five wordings the baseline refused. This runs the baseline lint (the lint at
    the first of fork/master, origin/master, master that resolves; HUB_VM_LINT_BASELINE_REF overrides) and the candidate over a generated
    corpus -- launch verb x playback word x local/host/here marker x venue word x negator / contrast word x joiner -- and fails on any
    refuse -> pass flip that no entry below explains. Every entry carries its reason and must still match at least one flip, so a stale
    entry cannot linger. A new exemption therefore has to be named here, in review, with a reason.

    Once a change has merged the baseline IS the candidate and the pin compares a lint with itself; it earns its keep on the next change,
    which is then judged against the merged lint.
    """

    ALLOWED_REFUSE_TO_PASS = (
        ("venue-asserted-in-the-local-marker-clause",
         lambda text, f: f.get("tpl") in ("marker-then-venue", "venue-then-marker", "marker-join-venue", "venue-join-marker")
         and f.get("J") in (None, ","),
         "r1b, Sol review of #401: one clause (a bare comma does not open a new clause) holds BOTH the local marker and an ASSERTED "
         "venue word, e.g. 'Run playback locally on Bachelor'. The work is routed to a venue, so it is not a local launch."),
        ("local-marker-negated-by-a-contrast-word-after-a-venue",
         lambda text, f: f.get("tpl") == "venue-contrast-marker" and f.get("M") == "locally",
         "r1b/r2: 'Run playback on Bachelor rather than / instead of / without / nor locally' -- the contrast complement is a strong "
         "negation, so `locally` there is the thing the prompt rules out. (Only `locally` flips: the host and 'here' markers were "
         "already passed by the baseline's sentence-wide weak-negation / venue exemption.)"),
        ("sol-probe-verbatim",
         lambda text, f: text == "Run playback locally on Bachelor through the venue chain.",
         "The probe sentence of the Sol review of #401, kept as an explicit row next to the rule that explains it."),
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hubvm-diff-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_refuse_to_pass_flip_against_the_baseline_is_unexplained(self):
        ref, opened, closed, size = _differential_flips(self.tmp)
        self.assertGreaterEqual(size, 1000, "the corpus shrank; the pin no longer samples the wording space")
        unexplained = [text for text, f in opened if not any(pred(text, f) for _, pred, _ in self.ALLOWED_REFUSE_TO_PASS)]
        self.assertEqual(
            [], unexplained,
            f"{len(unexplained)} wording(s) the baseline ({ref}) refuses now PASS and no ALLOWED_REFUSE_TO_PASS entry explains them. "
            "A safety guard may open only on purpose: fix the lint, or add an entry with its reason.")
        for name, pred, reason in self.ALLOWED_REFUSE_TO_PASS:
            self.assertTrue(reason.strip(), name)
            self.assertTrue(any(pred(text, f) for text, f in opened), f"allow-list entry {name!r} matches no flip any more; delete it")
        # The five regression wordings of the CoS review are corpus seeds and must not be among the flips at all.
        flipped = {text for text, _ in opened}
        for text in PromptLintTests.CLAUSE_MUST_REFUSE:
            self.assertNotIn(text, flipped, text)


@requires_pwsh
class InvokeLaneRefusalTests(unittest.TestCase):
    """A lane on the hub host whose prompt asks for local playback is refused before any process, run dir or receipt exists."""

    def _run(self, host: str, prompt: str, run_dir: Path, script: Path = INVOKE_LANE):
        env = dict(os.environ, COMPUTERNAME=host)
        env.pop("MLVAPP_HUB_PLAYBACK_CI_FIXTURE", None)
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), "-Lane", "sonnet", "-Prompt", prompt,
             "-WorkDir", str(ROOT), "-RunDir", str(run_dir), "-TimeoutSec", "60"],
            capture_output=True, text=True, timeout=180, env=env,
        )

    def test_a_copy_without_its_lint_refuses_every_lane_fail_closed(self):
        """Without the lint the host cannot be checked, so even a venue host with a harmless prompt is refused -- never run unchecked."""
        with tempfile.TemporaryDirectory(prefix="hubvm-nolint-") as tmp:
            script = Path(tmp) / "Invoke-Lane.ps1"
            script.write_bytes(INVOKE_LANE.read_bytes())
            run_dir = Path(tmp) / "run"
            done = self._run("BACHELOR", "Build and run the console tests.\n", run_dir, script)
            out = done.stdout + done.stderr
            self.assertNotEqual(0, done.returncode, out)
            self.assertIn("local-playback-lint-missing", out)
            self.assertIn("HUB_VM_LOCAL_PLAYBACK_REFUSED", out)
            self.assertFalse(run_dir.exists(), "the refusal must come before any run dir exists")

    def test_a_lint_without_the_host_list_refuses_every_lane_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="hubvm-nohosts-") as tmp:
            coord = Path(tmp) / "coordination"
            coord.mkdir()
            script = coord / "Invoke-Lane.ps1"
            script.write_bytes(INVOKE_LANE.read_bytes())
            (coord / LINT_PS1.name).write_bytes(LINT_PS1.read_bytes())
            run_dir = Path(tmp) / "run"
            done = self._run("BACHELOR", "Build and run the console tests.\n", run_dir, script)
            out = done.stdout + done.stderr
            self.assertNotEqual(0, done.returncode, out)
            self.assertIn("hub-hosts.json not found", out)
            self.assertFalse(run_dir.exists(), "the refusal must come before any run dir exists")

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
        # The Windows-only paths (%APPDATA% launchers, the job object type) come after it too, so the refusal runs on every OS (hosted ubuntu CI).
        for later in ("Write-Utf8NoBom $promptPath", "$hookCopyPath = ", "# ---------------------------------------------------------------- 0.1 pre-flight",
                      "Join-Path $env:APPDATA", "Add-Type -TypeDefinition"):
            at = text.find(later)
            # A renamed needle must fail here, not silently drop its ordering guarantee.
            self.assertNotEqual(-1, at, f"needle {later!r} no longer in Invoke-Lane.ps1; re-pin the ordering test")
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
                padded = " " + cfg["ciFixtureOverrideValue"] + " "
                self.assertEqual(cfg["refusalExitCode"], self._run_block(name, "VIRTUAL-TEN", padded).returncode, "a padded value is not the override")

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
