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


def _psq(path: object) -> str:
    """A path as a PowerShell single-quoted literal (an apostrophe in the path is doubled, never ends the string)."""
    return "'" + str(path).replace("'", "''") + "'"


def _write_probe(path: Path, body: str) -> None:
    # utf-8-sig, not ascii: a path with a non-ASCII character must reach pwsh intact instead of failing at write time.
    path.write_text(body, encoding="utf-8-sig")


def _lint_finding_counts(lint: Path, texts: list[str], work: Path, tag: str) -> "subprocess.Popen":
    """Start ONE pwsh that dot-sources `lint` and counts the findings of every text (one sentence per line). Returns the Popen."""
    src = work / f"{tag}-in.txt"
    src.write_text("\n".join(texts) + "\n", encoding="utf-8")
    probe = work / f"{tag}-probe.ps1"
    _write_probe(
        probe,
        ". " + _psq(lint) + "\n"
        "$out = New-Object System.Collections.Generic.List[int]\n"
        "foreach ($l in [System.IO.File]::ReadAllLines(" + _psq(src) + ")) { $out.Add(@(Get-LocalPlaybackPromptFindings -Text $l).Count) }\n"
        "ConvertTo-Json -InputObject @($out.ToArray()) -Compress\n",
    )
    return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _lint_word_lists(lint: Path, work: Path) -> dict[str, list[str]]:
    """The closed word lists the lint's exemptions depend on, read from the lint itself (Get-LocalPlaybackLintWordLists)."""
    probe = work / "lists-probe.ps1"
    _write_probe(probe, ". " + _psq(lint) + "\nConvertTo-Json -InputObject (Get-LocalPlaybackLintWordLists) -Compress -Depth 4\n")
    done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not done.stdout.strip():
        raise AssertionError(f"the lint exposes no word lists (Get-LocalPlaybackLintWordLists): {(done.stderr or done.stdout).strip()[:300]}")
    return {k: [str(x) for x in v] for k, v in json.loads(done.stdout.strip()).items()}


def _batch_counts(lint: Path, texts: list[str], work: Path, tag: str) -> list[int]:
    """Finding count of every text under `lint`, in ONE pwsh process (a text per line)."""
    proc = _lint_finding_counts(lint, texts, work, tag)
    try:
        return _collect_counts(proc, len(texts))
    finally:
        _kill(proc)


def _kill(proc: "subprocess.Popen") -> None:
    """Stop a probe that is still running and reap it (no orphaned pwsh after a timeout or an earlier failure)."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=30)
    except Exception:  # noqa: BLE001 - best effort cleanup only
        pass


def _collect_counts(proc: "subprocess.Popen", expected: int) -> list[int]:
    try:
        out, err = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise
    if proc.returncode != 0:
        raise AssertionError(f"lint probe failed: {err}")
    counts = json.loads(out.strip())
    if len(counts) != expected:
        raise AssertionError(f"lint probe returned {len(counts)} verdicts for {expected} sentences")
    return counts


# LIST-MEMBER DEFEATERS (r5; ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1 WITH the CoS amendment: one defeater row per
# member of EVERY closed word list an exemption depends on). The lists are read from the lint (Get-LocalPlaybackLintWordLists), so a
# word added to one is probed by construction -- nobody has to remember a row for it.
#
# Why each member of the lint's locus-preserving follow set keeps the venue as the PLACE the work runs. A member that is not named here
# fails test_every_follow_member_is_documented_locus_preserving: widening the set takes a reason, in review.
LOCUS_FOLLOW_REASONS = {
    "on": "a second routing preposition: 'on Bachelor on the venue chain' is still a venue",
    "via": "a second routing preposition",
    "through": "a second routing preposition ('on Bachelor through the venue chain')",
    "using": "a second routing preposition",
    "at": "a second routing preposition",
    "and": "coordinator; the clause ends here and the next clause is judged for a local host",
    "or": "coordinator; the clause ends here and the next clause is judged for a local host",
    "but": "coordinator; the clause ends here and the next clause is judged for a local host",
    "then": "sequencer; the clause ends here and the next clause is judged for a local host",
}
# Words that turn the venue into a tool, a source or a producer ("using Bachelor to fetch ...", "using Bachelor for the clip") or open a
# relative / purpose / manner complement. None is in the follow set; each must END the exemption. Words that are clause cuts of their own
# (when, after, because, ...) are judged by clause scope, not by this list.
REJECTED_FOLLOW_POOL = (
    "to", "for", "as", "that", "which", "with", "from", "so", "only", "also", "too", "just", "in", "by", "than", "where", "since", "if",
    "of", "into", "onto", "over", "under", "per", "against", "between", "within", "like", "including", "plus", "along", "alongside",
    "versus", "vs",
)
# Contexts a host reference is probed in. The defeater reads the reference itself, so a context must never be what makes a row refuse:
# every row is carried by `and` (a locus-preserving coordinator) or sits in the venue's own clause.
HOST_REF_PREPOSITIONS = ("on", "to", "onto", "into", "from", "at")


def _list_member_rows(lists: dict[str, list[str]]) -> list[tuple[str, bool, str]]:
    """(group, must_refuse, text) -- one row per member of every closed list the lint's exemptions depend on."""
    rows: dict[str, tuple[str, bool]] = {}

    def add(group: str, refuse: bool, text: str) -> None:
        rows.setdefault(text, (group, refuse))

    follow = lists["VenueFollowLocus"]
    for w in follow:
        add("follow-member-then-host", True, f"Run playback locally on Bachelor {w} this host.")
    for phrase in lists["VenueFollowPhrases"]:
        add("follow-phrase-control", False, f"Run playback locally on Bachelor {phrase}.")
        add("follow-phrase-then-host", True, f"Run playback locally on Bachelor {phrase} on this host.")
        add("follow-phrase-then-backreference", True, f"Run playback locally on Bachelor {phrase}, then run it here.")
    for w in REJECTED_FOLLOW_POOL:
        if w in follow:
            continue
        add("rejected-follow-word", True, f"Run playback locally using Bachelor {w} the clip.")
        add("rejected-follow-word-after-comma", True, f"Run playback locally using Bachelor, {w} the clip.")
    for det in lists["HostDeterminers"]:
        for noun in lists["HostNouns"]:
            add("host-reference-bare", True, f"Run playback locally on Bachelor and {det} {noun}.")
            add("host-reference-possessive", True, f"Run playback locally on Bachelor and {det} {noun}'s copy.")
            add("host-reference-same-clause", True, f"Run playback locally on Bachelor on {det} {noun}.")
            for prep in HOST_REF_PREPOSITIONS:
                add("host-reference-prep-det-noun", True, f"Run playback locally on Bachelor and {prep} {det} {noun}.")
    for name in lists["HostNames"]:
        add("host-name", True, f"Run playback locally on Bachelor and {name}.")
        add("host-name-same-clause", True, f"Run playback locally on Bachelor on {name}.")
    for w in lists["BackReferences"]:
        add("back-reference-follow-up", True, f"Run playback locally on Bachelor, then run {w} here.")
    for p in lists["RoutingPrepositionsLocally"]:
        add("e1-preposition-possessive", True, f"Run playback locally {p} Bachelor's clip.")
        add("e1-preposition-purpose", True, f"Run playback locally {p} Bachelor to fetch the clip.")
        add("e1-preposition-control", False, f"Run playback locally {p} Bachelor.")
    for p in lists["RoutingPrepositionsVenue"]:
        add("e3-preposition-then-host", True, f"Run playback {p} Bachelor on this host.")
        add("e3-preposition-possessive-then-host", True, f"Run playback {p} Bachelor's clip on this host.")
    for w in lists["VenueWords"]:
        add("venue-word-control", False, f"Run playback locally on {w}.")
        add("venue-word-then-host", True, f"Run playback locally on {w} on this host.")
        add("venue-word-possessive", True, f"Run playback locally on {w}'s clip.")
    for c in list(lists["ContrastWords"]) + ["not"]:
        add("contrast-word-then-local-host", True, f"Run playback on Bachelor {c} locally, then here.")
    return [(g, r, t) for t, (g, r) in rows.items()]


def _differential_corpus(seeds: tuple[str, ...], members: tuple = ()) -> dict[str, dict]:
    """text -> the fields it was built from: launch verb x playback word x local/host/here marker x venue word x negator / contrast word x joiner.
    `members` are the (group, must_refuse, text) list-member rows (r5); they join the corpus as tpl="list-member"."""
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
            # r3: a venue word that is NAMED but routes nothing -- subordinating / alternative / purpose connectives, and a comma clause
            for conn in ("since", "if", "as", "whenever", "in case", "to avoid", "with no", "or on", "or through", "because of"):
                add(f"{v} {pb} {m} {conn} {noun} is busy.", tpl="marker-connective-noun", M=m, V=noun, K=conn)
            add(f"{v} {pb} {m}, {noun} is not needed.", tpl="marker-comma-clause-noun", M=m, V=noun)
            # r3: a contrast word whose complement is some OTHER object and which only then reaches the marker
            for c1 in ("without", "not only", "instead of", "rather than", "skipping"):
                add(f"{v} {pb} {c1} audio {m}.", tpl="contrast-object-marker", M=m, C=c1)
                add(f"{v} {pb} {c1} {noun} but also {m}.", tpl="contrast-noun-but-also-marker", M=m, C=c1, V=noun)
            # r3: a leading prohibition reaches the comma clause after a contrast complement
            for neg in ("Never", "Do not"):
                add(f"{neg} {v.lower()} {pb} without {noun}, {m} or on this host.", tpl="neg-contrast-comma-marker", M=m, N=neg, V=noun)
    # r4: the defeater axis -- a venue word that is an attribute (possessive / noun) after a routing preposition, and a local-host
    # marker after the venue phrase in the same clause.
    for v, pb, m in itertools.product(verbs, pbs, markers):
        for prep in ("on", "using", "through"):
            for noun in nouns:
                add(f"{v} {pb} {m} {prep} {noun}'s clip.", tpl="defeater-possessive", M=m, V=noun)
                add(f"{v} {pb} {m} {prep} {noun} output.", tpl="defeater-attributive", M=m, V=noun)
        for ven in venues:
            for tail in (", on this host", ", then here", " on this host", " here"):
                add(f"{v} {pb} {m} {ven}{tail}.", tpl="defeater-trailing-host", M=m, V=ven, T=tail)
    for text in seeds:
        add(text, tpl="seed")
    for group, refuse, text in members:
        add(text, tpl="list-member", G=group, R=refuse)
    return rows


def _differential_flips(work: Path, baseline_text: str | None = None) -> tuple[str, list[tuple[str, dict]], list[tuple[str, dict]], int]:
    """Run baseline and candidate lints over the corpus. -> (baseline ref, refuse->pass rows, pass->refuse rows, corpus size).

    `baseline_text` replaces the git baseline (the identical-baseline row feeds the candidate's own text, which is the state of master
    right after the change merges). With no baseline ref and no override: a hosted CI job (fetch-depth: 0 in tests.yml) FAILS, because a
    pin that skips there guards nothing; a local clone without history skips."""
    if baseline_text is not None:
        ref, text = "candidate-itself", baseline_text
    else:
        base = _baseline_lint_source()
        if base is None:
            message = "no baseline ref resolves (fork/master, origin/master, master); the differential pin needs git history"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise AssertionError(message + " -- the hygiene jobs check out with fetch-depth: 0, so a missing ref is a broken checkout")
            raise unittest.SkipTest(message)
        ref, text = base
    baseline = work / "baseline-lint.ps1"
    baseline.write_text(text, encoding="utf-8")
    corpus = _differential_corpus(PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_PASS + PromptLintTests.PROBE_ROWS
                                  + tuple(text for _, _, text in PromptLintTests.DEFEATER_AXIS) + PromptLintTests.DEFEATER_AXIS_CONTROLS,
                                  members=tuple(_list_member_rows(_lint_word_lists(LINT_PS1, work))))
    texts = list(corpus)
    procs: list = []
    try:
        # Both launches sit inside the cleanup boundary: a failing second Popen must not leave the first unreaped.
        procs.append(_lint_finding_counts(baseline, texts, work, "base"))
        procs.append(_lint_finding_counts(LINT_PS1, texts, work, "cand"))
        before = _collect_counts(procs[0], len(texts))
        after = _collect_counts(procs[1], len(texts))
    finally:
        for proc in procs:
            _kill(proc)
    opened = [(t, corpus[t]) for t, b, a in zip(texts, before, after) if b > 0 and a == 0]
    closed = [(t, corpus[t]) for t, b, a in zip(texts, before, after) if b == 0 and a > 0]
    return ref, opened, closed, len(texts)


def _flip_summary(flips: list[tuple[str, dict]], limit: int = 3) -> str:
    """Group flips by template so a failure names the SHAPE that moved, with a few verbatim examples."""
    groups: dict[str, list[str]] = {}
    for text, fields in flips:
        groups.setdefault(str(fields.get("tpl")), []).append(text)
    return "; ".join(f"{tpl} x{len(rows)} e.g. {rows[:limit]}" for tpl, rows in sorted(groups.items()))


def _pin_problems(opened, closed, allow_open, allow_closed) -> list[str]:
    """What is wrong with a baseline -> candidate flip set. The allow-lists are CEILINGS: an entry that matches nothing is fine (the
    identical-baseline state, or a shape a later change has since removed); a flip that no entry explains is the failure."""
    problems = []
    for what, flips, allow in (("refuse -> PASS (the lint opened)", opened, allow_open), ("pass -> REFUSE (the lint closed)", closed, allow_closed)):
        for name, pred, reason in allow:
            if not reason.strip():
                problems.append(f"allow-list entry {name!r} has no reason")
        unexplained = [(t, f) for t, f in flips if not any(pred(t, f) for _, pred, _ in allow)]
        if unexplained:
            problems.append(f"{len(unexplained)} wording(s) flip {what} that no allow-list entry explains: {_flip_summary(unexplained)}")
    return problems


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
        _write_probe(
            probe,
            ". " + _psq(LINT_PS1) + "\n"
            "$f = @(Get-LocalPlaybackPromptFindings -Text (Get-Content -LiteralPath " + _psq(src) + " -Raw))\n"
            "ConvertTo-Json -InputObject @($f) -Compress\n",
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
        # r3 (Sol B1 + Opus V-01 on PR #448 r2 head b0b36922): every wording below was refused on master 8bcc2b3e and passed on r2, because
        # r2 guessed polarity from a word list. A venue word or a contrast word in the same clause is not a routing; only an enumerated
        # shape (see the lint header) exempts `locally`. The first two are Sol's, the last six are Opus's V-01 connectives.
        "Run playback without audio locally.",
        "Run playback not only on Bachelor but also locally.",
        "Run the arms locally since Bachelor is busy.",
        "Run playback locally if Bachelor is unreachable.",
        "Run playback locally to avoid the venue chain.",
        "Run the arms locally with no venue job.",
        "Run playback locally or on Bachelor.",
        "Run playback locally, the venue chain is not needed.",
        # siblings of the same two classes (a connective the lists never named; a contrast word whose complement is not the marker)
        "Run playback locally whenever Bachelor is busy.",
        "Run playback locally as Bachelor is busy.",
        "Run the arms locally in case the venue chain fails.",
        "Run playback without the overlay locally.",
        "Launch MLVApp without scopes locally.",
        "Run playback on Bachelor but also locally.",
        # r4 (CoS review of PR #448 r3 head a29f9b5e, pr-448.md on bus 4d1b327e): master refused all six and r3 passed them -- E1 / E3 read a
        # venue word after a routing preposition without checking it is WHERE THE WORK RUNS (a possessive or an attributive noun), and
        # the venue exemption survived a later local-host marker in the same clause.
        "Run the arms locally using Bachelor's clip on this host.",
        "Run playback locally through the venue chain's clip on this host.",
        "Run playback locally using Bachelor's clip.",
        "Launch MLVApp locally on the Bachelor clip.",
        "Run playback locally using the venue chain output.",
        "Run playback locally on Bachelor, on this host.",
        # r5 (CoS review of PR #448 r4 head c8d5220a, pr-448.md on bus 6cb7a0f6): master refused every row and r4 passed them. The venue
        # follow set let an instrument / purpose complement (to, for, as, that, with, so, from, only ...) follow the venue name, and the
        # host defeater only knew `on this host` / `here`. The first row is the 2026-10-11 incident shape almost word for word.
        "Run playback locally using the venue chain to copy the clip to this host.",
        "Run playback locally using the venue chain to fetch the clip.",
        "Run the arms locally using Bachelor as the clip source.",
        "Run playback locally using Bachelor for the clip.",
        "Run playback locally using Bachelor only for the clip copy.",
        "Run playback locally using the venue chain that produced the clip.",
        "Run playback locally using Bachelor so the hub has the clip.",
        "Run playback locally using Bachelor and the hub copy.",
        "Run playback locally using Bachelor with this host's copy.",
        "Run playback locally via Bachelor to this host.",
        "Run the arms locally using Bachelor from this host.",
        "Run playback locally on Bachelor on my machine.",
        "Run playback locally on Bachelor, then run it here.",
    )
    CLAUSE_MUST_PASS = (
        "Run playback locally on Bachelor through the venue chain.",
        "Run playback on Bachelor through the venue chain, not on this host.",
        "Run playback through the venue chain and read the log here.",
        "Never run playback locally, nor on this host.",
        "Never run playback locally and on this host.",
        "Run the arms on Bachelor through the venue chain and never locally.",
        "Run playback on Bachelor rather than locally.",
        # r3 (Opus HUB-VM-LINT-NOINHERIT-PROHIBITION-1): a leading prohibition scopes the comma clause after a contrast complement.
        "Never run playback without the venue chain, locally or on this host.",
        "Run playback on Bachelor, never locally.",
        "Run playback on Bachelor instead of locally.",
    )

    # DEFEATER AXIS (r4; ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1, bus 4d1b327e). A shape (E1 / E2 / E3 in the lint
    # header) is admitted only with a row per kind of defeater that must STILL be refused: the venue word as a possessive or an
    # attributive noun (it names a venue without being where the work runs), a local-host marker after the exempting phrase, and venue +
    # host in one clause. (shape, kind, text). E2 has no possessive / attributive kind: its exemption comes from the contrast word,
    # not from a venue word.
    DEFEATER_KINDS = {
        "E1": ("possessive-venue", "attributive-venue", "trailing-local-host", "venue-and-host-one-clause"),
        "E2": ("trailing-local-host", "venue-and-host-one-clause"),
        "E3": ("possessive-venue", "attributive-venue", "trailing-local-host", "venue-and-host-one-clause"),
    }
    DEFEATER_AXIS = (
        ("E1", "possessive-venue", "Run the arms locally using Bachelor's copy of the clip."),
        ("E1", "possessive-venue", "Run playback locally through the venue chain's clip."),
        ("E1", "attributive-venue", "Run playback locally on Bachelor files."),
        ("E1", "attributive-venue", "Run playback locally using the Bachelor clip."),
        ("E1", "trailing-local-host", "Run playback locally on Bachelor, on this host."),
        ("E1", "trailing-local-host", "Run playback locally on Bachelor, then here."),
        ("E1", "venue-and-host-one-clause", "Run playback locally on Bachelor on this host."),
        ("E1", "venue-and-host-one-clause", "Run playback locally through the venue chain here."),
        ("E2", "trailing-local-host", "Run playback on Bachelor rather than locally, here."),
        ("E2", "trailing-local-host", "Run playback on Bachelor instead of locally, then here."),
        ("E2", "venue-and-host-one-clause", "Run playback on Bachelor rather than locally on this host."),
        ("E2", "venue-and-host-one-clause", "Run playback on Bachelor rather than locally, on this host."),
        ("E3", "possessive-venue", f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor's clip."),
        ("E3", "possessive-venue", "Run playback on Bachelor's clip on this host."),
        ("E3", "attributive-venue", f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on the Bachelor clip."),
        ("E3", "attributive-venue", "Run playback through the venue chain output here."),
        ("E3", "trailing-local-host", f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor, on this host."),
        ("E3", "trailing-local-host", f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor here."),
        ("E3", "venue-and-host-one-clause", "Run playback through the venue chain on this host."),
        ("E3", "venue-and-host-one-clause", "Run playback on Bachelor through the venue chain here."),
    )
    # The same venue phrases, with no defeater, that must keep passing (the exemptions are narrowed, never removed).
    DEFEATER_AXIS_CONTROLS = (
        "Run playback locally on Bachelor through the venue chain.",
        "Run playback locally using Bachelor.",
        "Run playback on Bachelor rather than locally.",
        f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor.",
        f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu through the venue chain from here.",
        "Run playback through the venue chain from here.",
    )

    # Rows that are in no catalogue tuple above but belong in the differential corpus as seeds.
    PROBE_ROWS = (
        "Run playback through the venue chain and copy the log locally.",
    )

    def test_defeater_axis_covers_every_kind_of_every_shape(self):
        for shape, kinds in self.DEFEATER_KINDS.items():
            have = {kind for s, kind, _ in self.DEFEATER_AXIS if s == shape}
            self.assertEqual(set(kinds), have, f"shape {shape}: a defeater kind has no row (or a row names an unknown kind)")

    def test_red_defeater_axis_rows_are_refused(self):
        for shape, kind, text in self.DEFEATER_AXIS:
            with self.subTest(shape=shape, kind=kind, text=text):
                self.assertTrue(self._findings(text), text)

    def test_green_defeater_axis_controls_pass(self):
        for text in self.DEFEATER_AXIS_CONTROLS:
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)

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
        _write_probe(
            probe,
            ". " + _psq(LINT_PS1) + "\n"
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
        )
        env = {k: v for k, v in os.environ.items() if k != "MLVAPP_HUB_PLAYBACK_CI_FIXTURE"}
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(
            {"hub": True, "hubLower": True, "bachelor": False, "um": False, "partial": False, "empty": False, "truthy": True, "padded": True, "override": False},
            json.loads(done.stdout.strip()),
        )


@requires_pwsh
class ListMemberDefeaterTests(unittest.TestCase):
    """r5 (CoS prevention note on PR #448 r4, ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1 WITH the CoS amendment).

    r1b, r2, r3 and r4 each passed their own pins and broke on an outside probe, four times, because the pins sampled the SHAPES somebody
    thought of while the exemption depended on a closed WORD LIST nobody probed member by member (r4: a follow set that let `to`, `for`,
    `as`, `that`, `with`, `so` through; a host marker only ever probed as `on this host`). This class reads every such list from the lint
    and generates one row per member -- so an unprobed member cannot exist, and a word added to a list is tested by construction."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hubvm-members-"))
        cls.lists = _lint_word_lists(LINT_PS1, cls.tmp)
        cls.rows = _list_member_rows(cls.lists)
        counts = _batch_counts(LINT_PS1, [t for _, _, t in cls.rows], cls.tmp, "members")
        cls.verdicts = {t: c for (_, _, t), c in zip(cls.rows, counts)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_lint_exposes_every_list_the_exemptions_depend_on(self):
        for key in ("VenueWords", "RoutingPrepositionsLocally", "RoutingPrepositionsVenue", "VenueFollowLocus", "VenueFollowPhrases",
                    "HostDeterminers", "HostNouns", "HostNames", "BackReferences", "ContrastWords"):
            self.assertTrue(self.lists.get(key), f"list {key} is missing or empty")

    def test_the_lint_builds_its_patterns_from_those_lists(self):
        """The exposed lists must be the lists the lint USES, not a second copy that can drift: the findings function reads them, and the
        literal alternation they replaced is gone."""
        text = LINT_PS1.read_text(encoding="utf-8")
        body = text[text.index("function Get-LocalPlaybackPromptFindings"):]
        self.assertIn("Get-LocalPlaybackLintWordLists", body)
        for key in ("VenueFollowLocus", "VenueFollowPhrases", "HostDeterminers", "HostNouns", "HostNames", "BackReferences", "ContrastWords",
                    "RoutingPrepositionsLocally", "RoutingPrepositionsVenue", "VenueWords"):
            self.assertIn(f"$lists.{key}", body, f"the findings function does not read list {key}")
        self.assertNotRegex(body, r"\$venueFollow\s*=\s*'", "a literal follow alternation came back")

    def test_every_follow_member_is_documented_locus_preserving(self):
        undocumented = [w for w in self.lists["VenueFollowLocus"] if w not in LOCUS_FOLLOW_REASONS]
        self.assertEqual([], undocumented, "a word was added to the venue follow set with no documented reason (LOCUS_FOLLOW_REASONS)")
        for w in self.lists["VenueFollowLocus"]:
            self.assertTrue(LOCUS_FOLLOW_REASONS[w].strip(), w)
        # an instrument / purpose / source complement may never be in the set
        self.assertEqual([], [w for w in REJECTED_FOLLOW_POOL if w in self.lists["VenueFollowLocus"]])
        for w in ("to", "for", "as", "that", "which", "with", "so", "only", "from"):
            self.assertNotIn(w, self.lists["VenueFollowLocus"])

    def test_the_row_set_is_not_hollow(self):
        groups = {g for g, _, _ in self.rows}
        for g in ("follow-member-then-host", "rejected-follow-word", "rejected-follow-word-after-comma", "host-reference-bare",
                  "host-reference-possessive", "host-reference-same-clause", "host-reference-prep-det-noun", "host-name",
                  "back-reference-follow-up", "e1-preposition-possessive", "e3-preposition-then-host", "venue-word-control",
                  "venue-word-then-host", "venue-word-possessive", "contrast-word-then-local-host"):
            self.assertIn(g, groups, f"no rows generated for {g}")
        # preposition x determiner x noun is the full product, not a sample
        product = len(HOST_REF_PREPOSITIONS) * len(self.lists["HostDeterminers"]) * len(self.lists["HostNouns"])
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "host-reference-prep-det-noun"), product)

    def _group(self, group: str, refuse: bool) -> None:
        rows = [t for g, r, t in self.rows if g == group]
        self.assertTrue(rows, f"no rows in {group}")
        wrong = [t for t in rows if (self.verdicts[t] > 0) != refuse]
        self.assertEqual([], wrong, f"{group}: rows the lint {'passed' if refuse else 'refused'} that it must {'refuse' if refuse else 'pass'}")

    def test_red_every_follow_member_still_defeated_by_a_host(self):
        self._group("follow-member-then-host", True)
        self._group("follow-phrase-then-host", True)
        self._group("follow-phrase-then-backreference", True)

    def test_red_every_rejected_follow_word_ends_the_exemption(self):
        self._group("rejected-follow-word", True)
        self._group("rejected-follow-word-after-comma", True)

    def test_red_every_host_reference_defeats(self):
        for g in ("host-reference-bare", "host-reference-possessive", "host-reference-same-clause", "host-reference-prep-det-noun",
                  "host-name", "host-name-same-clause"):
            self._group(g, True)

    def test_red_every_back_reference_follow_up_defeats(self):
        self._group("back-reference-follow-up", True)

    def test_red_every_routing_preposition_needs_a_locus(self):
        for g in ("e1-preposition-possessive", "e1-preposition-purpose", "e3-preposition-then-host", "e3-preposition-possessive-then-host"):
            self._group(g, True)

    def test_red_every_venue_word_is_a_locus_only_as_itself(self):
        self._group("venue-word-then-host", True)
        self._group("venue-word-possessive", True)

    def test_red_every_contrast_word_keeps_a_trailing_local_host_defeated(self):
        self._group("contrast-word-then-local-host", True)

    def test_green_the_members_still_exempt_what_they_are_for(self):
        """The narrowed lists must not be dead: every venue word and every routing preposition still passes in its plain form."""
        self._group("venue-word-control", False)
        self._group("e1-preposition-control", False)
        self._group("follow-phrase-control", False)


@requires_pwsh
class LintDifferentialPinTests(unittest.TestCase):
    """HUB-VM-LINT-CLAUSE-SCOPE-1 r2 (CoS prevention note). A catalogue only proves the rows somebody thought of; r1b added an exemption
    (a venue word read before `locally`) that silently opened five wordings the baseline refused. This runs the baseline lint (the lint at
    the first of fork/master, origin/master, master that resolves; HUB_VM_LINT_BASELINE_REF overrides) and the candidate over a generated
    corpus -- launch verb x playback word x local/host/here marker x venue word x negator / contrast word x joiner / connective -- and
    fails on any flip, in EITHER direction, that no entry below explains. Every entry carries its reason. The lists are ceilings (r3): an
    entry that matches no flip is fine, so the state right after a merge -- baseline == candidate, zero flips -- passes
    (test_identical_baseline_passes). A new exemption has to be named here, in review, with a reason.

    Once a change has merged the baseline IS the candidate and the pin compares a lint with itself; it earns its keep on the next change,
    which is then judged against the merged lint. In hosted CI (GITHUB_ACTIONS, fetch-depth: 0) a missing baseline ref FAILS the pin;
    only a local clone without history skips it.
    """

    # Both lists are CEILINGS: an entry that explains no flip is fine (identical baseline, or a shape removed later); a flip no entry
    # explains fails. An entry names the lint's enumerated shape (E1 / E2 / E3 in its header) or the review round that required it.
    ALLOWED_REFUSE_TO_PASS = (
        ("E1-locally-routed-onto-a-venue",
         lambda text, f: (f.get("tpl") == "marker-then-venue" and f.get("M") == "locally")
         or text in ("Run playback locally on Bachelor through the venue chain.", "Run playback locally using Bachelor."),
         "E1, Sol review of #401: `locally` immediately followed by a routing preposition and a venue word ('Run playback locally on "
         "Bachelor', 'locally through the venue chain') names the venue the work runs on, so it is not a local launch."),
        ("E1-per-member-plain-forms",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") in ("e1-preposition-control", "venue-word-control"),
         "r5, ListMemberDefeaterTests: the plain E1 form of every member of RoutingPrepositionsLocally and VenueWords ('Run playback "
         "locally via Bachelor.', '... on ultra-magnus.') -- the same E1 shape as the entry above, one row per list member, nothing "
         "after the venue name."),
        ("E1-locally-on-a-venue-from-here",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") == "follow-phrase-control",
         "r4 design note (CoS), listed in r5: 'Run playback locally on Bachelor from here.' -- `from here` says where a venue job is "
         "STARTED from, not where it runs, so it is a locus-preserving phrase (VenueFollowPhrases). Every defeater of it is refused: "
         "'... from here on this host', '... from here, then run it here' (follow-phrase rows)."),
        ("E2-marker-negated-by-the-contrast-word-before-it",
         lambda text, f: f.get("tpl") == "venue-contrast-marker" and f.get("M") == "locally",
         "E2, r1b/r2: 'Run playback on Bachelor rather than / instead of / without / nor / , not locally' -- the marker is the first "
         "thing of the contrast complement, so it is what the prompt rules out. (Only `locally` flips: the host and 'here' markers were "
         "already passed by the baseline's sentence-wide weak-negation / venue exemption.)"),
    )
    _CLAUSE_SCOPED_TPLS = ("marker-contrast-venue", "marker-contrast-marker", "contrast-first", "marker-join-venue", "venue-join-marker")
    ALLOWED_PASS_TO_REFUSE = (
        ("clause-scope-of-a-negation-or-venue-word",
         lambda text, f: f.get("tpl") in LintDifferentialPinTests._CLAUSE_SCOPED_TPLS
         or (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE),
         "r1b + r2, CoS review of #448 and Sol review of #401: a negation or a venue word exempts only the clause it sits in; the "
         "baseline's sentence-wide exemption passed 'Do not run the venue chain, run playback locally', 'Run playback on Bachelor and on "
         "this host', 'Run playback locally, never on Bachelor' and the like."),
        ("venue-and-local-host-in-one-clause",
         lambda text, f: (f.get("tpl") in ("marker-then-venue", "venue-then-marker", "defeater-trailing-host")
                          and f.get("M") in ("on this host", "here"))
         or (f.get("tpl") == "list-member" and f.get("G") in ("e3-preposition-then-host", "e3-preposition-possessive-then-host")),
         "r4, CoS review of #448 r3: a clause that names both a venue and the local host ('Run playback on Bachelor on this host', "
         "'... through the venue chain here') is refused; a venue qualification never exempts a clause that also holds a local-host "
         "marker (a defeater). The baseline exempted the host / here marker on any venue word in the sentence."),
        ("venue-word-that-is-not-the-locus",
         lambda text, f: f.get("tpl") in ("defeater-possessive", "defeater-attributive") and f.get("M") in ("on this host", "here"),
         "r4: 'on Bachelor's clip', 'through the venue chain output' -- the venue word is a possessive or an attributive noun, not where "
         "the work runs, so it is no routing phrase; the baseline passed the host / here marker on any venue word in the sentence."),
        ("contrast-word-then-local-host-per-member",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") == "contrast-word-then-local-host",
         "r4 'also closed' class, one row per member of ContrastWords (r5): 'Run playback on Bachelor rather than locally, then here.' -- "
         "a `here` clause after an E2 complement is a local-host marker and defeats the exemption. The baseline passed the `not` member "
         "through its sentence-wide weak-negation exemption."),
        ("defeater-axis-seeds",
         lambda text, f: f.get("tpl") == "seed" and text in tuple(t for _, _, t in PromptLintTests.DEFEATER_AXIS),
         "r4: the defeater-axis rows (PromptLintTests.DEFEATER_AXIS) are refused by construction; the E3 / host rows of them were "
         "passed by the baseline's sentence-wide venue exemption."),
        ("bare-here-marker",
         lambda text, f: f.get("tpl") == "bare" and f.get("M") == "here",
         "r1b, CoS review of #401: 'Run playback here.' -- the baseline has no `here` rule."),
        ("venue-named-but-not-routed",
         lambda text, f: f.get("tpl") == "marker-connective-noun" and f.get("M") in ("on this host", "here"),
         "E3, r3: a venue word outside a routing phrase ('since Bachelor is busy', 'to avoid the venue chain', 'because of the venue "
         "chain') exempts nothing; the baseline passed the host / here marker on any venue word in the sentence."),
        ("contrast-complement-that-is-not-the-marker",
         lambda text, f: f.get("tpl") in ("contrast-object-marker", "contrast-noun-but-also-marker"),
         "E2, r3: 'without audio on this host', 'not only Bachelor but also on this host' -- the marker is not the head of the "
         "complement, so nothing negates it; the baseline passed on any weak negation or venue word in the sentence."),
        ("hardening-log-handling-tail",
         lambda text, f: f.get("tpl") == "handling-tail" and f.get("M") in ("on this host", "here"),
         "r1b over-refusal kept on purpose (card HUB-VM-LINT-LOG-COPY-OVERREFUSAL-1): 'copy the log on this host' after a venue run "
         "is refused like its `locally` twin; a handling-verb list would open 'Copy the clip locally and run playback.'"),
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hubvm-diff-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_unlisted_flip_against_the_baseline(self):
        ref, opened, closed, size = _differential_flips(self.tmp)
        self.assertGreaterEqual(size, 1000, "the corpus shrank; the pin no longer samples the wording space")
        problems = _pin_problems(opened, closed, self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE)
        self.assertEqual(
            [], problems,
            f"baseline {ref}: a safety guard may open or close only on purpose -- fix the lint, or add an allow-list entry with its reason.")
        # Every catalogue row that must be refused is a corpus seed and must not be among the flips at all.
        flipped = {text for text, _ in opened}
        for text in PromptLintTests.CLAUSE_MUST_REFUSE:
            self.assertNotIn(text, flipped, text)

    def test_identical_baseline_passes(self):
        """Once this change is on master the baseline IS the candidate: no flips, and the pin must pass (it failed on r2, whose entries
        were each required to match a flip)."""
        ref, opened, closed, _ = _differential_flips(self.tmp, baseline_text=LINT_PS1.read_text(encoding="utf-8"))
        self.assertEqual("candidate-itself", ref)
        self.assertEqual(([], []), ([t for t, _ in opened], [t for t, _ in closed]))
        self.assertEqual([], _pin_problems(opened, closed, self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE))

    def test_the_pin_bites_on_an_unlisted_flip_in_either_direction(self):
        row = ("Run playback locally since Bachelor is busy.", {"tpl": "marker-connective-noun"})
        self.assertTrue(_pin_problems([row], [], self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE))
        self.assertTrue(_pin_problems([], [row], self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE))
        self.assertEqual([], _pin_problems([], [], self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE),
                         "allow-list entries are a ceiling, never a required set")
        self.assertTrue(_pin_problems([], [], (("no-reason", lambda t, f: True, " "),), ()))


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
