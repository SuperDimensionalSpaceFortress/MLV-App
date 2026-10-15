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


# PREVIOUS-HEAD DIFFERENTIAL (r7; CoS Prevention on PR #448 a595464f, ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1).
# The differential against master cannot see a narrowing that undoes the PREVIOUS head's own safety gains: a row the previous head
# refused and master passes shows as zero flips against master. Every round therefore also runs the corpus against the lint of the PR
# head the round STARTED from. EACH ROUND MOVES THIS to the head it started from (r7 started from r6 4957fff0). It is checked to be an
# ancestor of HEAD, so a rebase that drops it fails loudly in CI instead of silently weakening the gate.
PREVIOUS_PR_HEAD = "4957fff04b81f224840d37efb4e780cde9352ed6"


def _previous_head_lint_source() -> tuple[str, str] | None:
    """(ref, text) of the lint at PREVIOUS_PR_HEAD, or None when that commit is not in this clone."""
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"{PREVIOUS_PR_HEAD}:{LINT_REL}"], capture_output=True, timeout=60)
    if done.returncode == 0 and done.stdout:
        return PREVIOUS_PR_HEAD[:8], done.stdout.decode("utf-8")
    return None


def _require_previous_head_lint() -> tuple[str, str]:
    """(ref, text) of the lint at PREVIOUS_PR_HEAD. Skips once master already carries this lint (a squash-merged PR head is no longer
    reachable, and the comparison is moot); a missing commit FAILS in hosted CI (fetch-depth: 0) and skips in a clone without history."""
    base = _baseline_lint_source()
    if base is not None and base[1].replace("\r\n", "\n") == LINT_PS1.read_text(encoding="utf-8").replace("\r\n", "\n"):
        raise unittest.SkipTest(f"{base[0]} already carries this lint; the previous PR head is no longer a baseline")
    prev = _previous_head_lint_source()
    if prev is None:
        message = f"previous PR head {PREVIOUS_PR_HEAD[:8]} is not in this clone; the previous-head differential needs the PR history"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise AssertionError(message + " -- the hygiene jobs check out with fetch-depth: 0, so a missing commit is a stale PREVIOUS_PR_HEAD or a broken checkout")
        raise unittest.SkipTest(message)
    return prev


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
# r6 axes. The joiners are the ones the clause splitter knows (a comma, a semicolon, a dash, then / and / but / so); each row is
# "<venue-routed launch><joiner> <verb> <object> <tail>" where the object is a PLAIN noun, never a back-reference word.
LATER_CLAUSE_JOINERS = (",", ", then", ";", "; then", " - then", " and then", " but", ", so")
LATER_LAUNCH_VERBS = ("run", "play", "open", "start", "launch", "execute")
LATER_OBJECTS = ("the clip", "the build", "this", "it")
LATER_HOST_TAILS = ("here", "on this host", "on my machine", "locally")
HOST_MODIFIER_POOL = ("same", "very", "Windows", "physical")
# r7 axes (CoS review of PR #448 r5 head a595464f, pr-448.md on bus eefcc03): a direct launcher line (Rule 4) carries no local marker,
# so the venue phrase is the only evidence of where it runs. A host noun introduced by a LOCUS preposition (it says where the work runs)
# or a host name defeats the venue exemption there; a host noun that is a DESTINATION (to / into / from) does not.
# (the locus prepositions are read from the lint: Get-LocalPlaybackLintWordLists .HostLocusPrepositions)
LAUNCH_DESTINATION_PREPOSITIONS = ("to", "into", "from")
LAUNCH_LINES = (
    f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu",
    f"& ./tools/profiling/{SMOKE_SCRIPT} -Backend cpu",
    f"Run tools/profiling/{SMOKE_SCRIPT} -Backend cpu",
    f"{APP} clip.mlv {SMOKE_MODE}",
)
LAUNCH_VENUE_PHRASES = ("on Bachelor", "through the venue chain")


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
    # r6 axes (CoS Blocker 1 on r5, fail closed): a later clause with a launch verb that names the local host defeats the exemption
    # whatever its object is; `;` is a clause boundary like `,`; the host vocabulary takes modifiers, hyphen / space / case variants,
    # a bare preposition + noun and the loopback names.
    for j in LATER_CLAUSE_JOINERS:
        for verb in LATER_LAUNCH_VERBS:
            for obj in LATER_OBJECTS:
                for tail in LATER_HOST_TAILS:
                    add("later-clause-then-host", True, f"Run playback locally on Bachelor{j} {verb} {obj} {tail}.")
    for det in lists["HostDeterminers"]:
        for noun in lists["HostNouns"]:
            for mod in HOST_MODIFIER_POOL:
                add("host-reference-modifier", True, f"Run playback locally on Bachelor and on {det} {mod} {noun}.")
    for noun in lists["HostNouns"]:
        add("host-reference-two-modifiers", True, f"Run playback locally on Bachelor and on this very same {noun}.")
        for suffix in ("-VM", "-vm", " VM"):
            add("host-reference-vm-suffix", True, f"Run playback locally on Bachelor and on the {noun}{suffix}.")
    for word in list(lists["HostNouns"]) + list(lists["HostNames"]):
        if " " not in word:
            continue
        for variant in (word.replace(" ", "-"), word.replace(" ", ""), word.replace(" ", "-").upper(), word.replace(" ", " ").title().replace(" ", "-")):
            add("host-reference-spelling-variant", True, f"Run playback locally on Bachelor and on the {variant}.")
            add("host-name-spelling-variant", True, f"Run playback locally on Bachelor and on {variant}.")
    for prep in lists["HostPrepositions"]:
        for noun in lists["HostNouns"]:
            add("host-reference-bare-preposition", True, f"Run playback locally on Bachelor and {prep} {noun}.")
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
    # r7 axes (Rule 4, direct launch; CoS Improvement on a595464f). Every launcher form x venue phrase x host reference. The full
    # determiner x noun x locus-preposition product runs on the first launcher form (the pwsh -File line); the other forms take every
    # noun with `this` and every host name, so each launcher shape and each list member is on a Rule 4 line at least once.
    first = LAUNCH_LINES[0]
    for ven in LAUNCH_VENUE_PHRASES:
        for prep in lists["HostLocusPrepositions"]:
            for det in lists["HostDeterminers"]:
                for noun in lists["HostNouns"]:
                    add("launch-host-locus-det-noun", True, f"{first} {ven} and {prep} {det} {noun}.")
            for noun in lists["HostNouns"]:
                add("launch-host-locus-bare-noun", True, f"{first} {ven} and {prep} {noun}.")
                add("launch-host-locus-vm-suffix", True, f"{first} {ven} and {prep} the {noun}-VM.")
                for line in LAUNCH_LINES[1:]:
                    add("launch-host-locus-other-forms", True, f"{line} {ven} and {prep} this {noun}.")
        for det in lists["HostDeterminers"]:
            for noun in lists["HostNouns"]:
                add("launch-host-locus-same-clause", True, f"{first} {ven} on {det} {noun}.")
                add("launch-host-locus-possessive", True, f"{first} {ven} using {det} {noun}'s GPU.")
                add("launch-host-locus-later-clause", True, f"{first} {ven}, then run it on {det} {noun}.")
            for mod in HOST_MODIFIER_POOL:
                add("launch-host-locus-modifier", True, f"{first} {ven} and on {det} {mod} {noun}.")
        for line in LAUNCH_LINES:
            for name in lists["HostNames"]:
                add("launch-host-name", True, f"{line} {ven} and on {name}.")
                add("launch-host-name-bare", True, f"{line} {ven} and {name}.")
            for word in list(lists["HostNouns"]) + list(lists["HostNames"]):
                if " " not in word:
                    continue
                for variant in (word.replace(" ", "-"), word.replace(" ", ""), word.replace(" ", "-").upper()):
                    add("launch-host-spelling-variant", True, f"{line} {ven} and on the {variant}.")
            add("launch-venue-control", False, f"{line} {ven}.")
            add("launch-venue-control", False, f"The venue job calls {line} {ven}.")
            add("launch-venue-control", False, f"{line} {ven} and posts the result to the hub.")
            add("launch-venue-control", False, f"{line} {ven}, then post the log to the hub.")
        for prep in LAUNCH_DESTINATION_PREPOSITIONS:
            for det in lists["HostDeterminers"]:
                for noun in lists["HostNouns"]:
                    add("launch-host-destination-control", False, f"{first} {ven} and copies the clip {prep} {det} {noun}.")
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


def _differential_flips(work: Path, baseline_text: str | None = None, previous_head: bool = False) -> tuple[str, list[tuple[str, dict]], list[tuple[str, dict]], int]:
    """Run baseline and candidate lints over the corpus. -> (baseline ref, refuse->pass rows, pass->refuse rows, corpus size).

    `baseline_text` replaces the git baseline (the identical-baseline row feeds the candidate's own text, which is the state of master
    right after the change merges). With no baseline ref and no override: a hosted CI job (fetch-depth: 0 in tests.yml) FAILS, because a
    pin that skips there guards nothing; a local clone without history skips.
    `previous_head` takes the baseline from PREVIOUS_PR_HEAD instead of master (r7). That commit is not on master once the PR is
    squash-merged, so when master's lint already IS the candidate the comparison is moot and the pin skips."""
    if baseline_text is not None:
        ref, text = "candidate-itself", baseline_text
    elif previous_head:
        ref, text = _require_previous_head_lint()
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
    corpus = _differential_corpus(PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_PASS + PromptLintTests.CLAUSE_MUST_PASS_R6
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R7 + PromptLintTests.CLAUSE_MUST_PASS_R7
                                  + PromptLintTests.PROBE_ROWS
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
        # r6 (CoS review of PR #448 r5 head eb3d5f81, pr-448.md on bus ee73df4): master refused every row and r5 passed them. (a) a later
        # launch clause with a plain object, (b) a semicolon joiner (it was a sentence boundary), (c) host vocabulary the r5 pattern
        # could not read (modifiers, hub-VM, system, device, a bare `hub`, an address).
        "Run playback locally on Bachelor, then play the clip here.",
        "Run playback locally on Bachelor, then run this here.",
        "Run playback locally on Bachelor, then open the clip here.",
        "Run playback locally on Bachelor, then run the result here.",
        "Run playback locally on Bachelor, then start the player here.",
        "Run playback locally on Bachelor, then run the clip on my machine.",
        "Run playback locally on Bachelor; run it here.",
        "Run playback locally on Bachelor; then run it here.",
        "Run playback locally on Bachelor; run it on this host.",
        "Run playback locally on Bachelor; play the clip on this host.",
        "Run playback locally on Bachelor and on the hub-VM.",
        "Run playback locally on Bachelor and on the HUB-VM.",
        "Run playback locally on Bachelor and on this same host.",
        "Run playback locally on Bachelor and on this very machine.",
        "Run playback locally on Bachelor and on this Windows box.",
        "Run playback locally on Bachelor and on the current machine.",
        "Run playback locally on Bachelor and on our own box.",
        "Run playback locally on Bachelor and on this system.",
        "Run playback locally on Bachelor and on this device.",
        "Run playback locally on Bachelor and on hub.",
        "Run playback locally on Bachelor and on 127.0.0.1.",
    )
    # r7 (CoS review of PR #448 r5 head a595464f, pr-448.md on bus eefcc03, first Improvement; the hub's probe of r6 4957fff0 found 10
    # of these still passing): Rule 4 (a direct launcher line, no `locally`) with a SECOND, local locus after the venue. r5 eb3d5f81
    # refused them, r5-follow-up a595464f narrowed Rule 4 to the r4 defeaters to keep a destination (`posts the result to the hub`) green
    # and gave them back, r6 did not restore them. Master passes every row (its venue exemption is sentence-wide), so each is a
    # pass -> refuse flip vs master, listed in ALLOWED_PASS_TO_REFUSE ("direct-launch-second-local-locus"). The first ten are the CoS
    # note's rows verbatim (hub copy: lane-HUB-TICK-20261015T1258Z/cos-448-a595-rule4-rows.txt).
    CLAUSE_MUST_REFUSE_R7 = (
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on my machine.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on localhost.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on this workstation.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on this desktop.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on my laptop.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on this PC.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor using this host's GPU.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor, then run it on my machine.",
        f"& ./tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on my machine.",
        f"Run tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on my machine.",
        # the note's other named rows: the MLVApp launcher form, and the HUB-VM spelling that passed on every version through r6
        f"{APP} clip.mlv {SMOKE_MODE} on Bachelor and on localhost.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and on the HUB-VM.",
        # the r6 tip's stated, fail-closed difference (the note's second Improvement): a purpose / destination complement after the
        # venue name (`via Bachelor to this host`) makes the venue an instrument, not the place the work runs -- refused since r5, kept.
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu via Bachelor to this host.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu through the venue chain to this host.",
    )
    # r7 CONTROLS: a host noun that is a DESTINATION on a venue launcher line stays passing (both a595464f controls and the copy row).
    CLAUSE_MUST_PASS_R7 = (
        f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and posts the result to the hub.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and copies the clip to this host.",
        f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor, then post the log to the hub.",
        f"{APP} clip.mlv {SMOKE_MODE} on Bachelor and posts the result to the hub.",
        f"& ./tools/profiling/{SMOKE_SCRIPT} -Backend cpu through the venue chain and fetches the log from the hub VM.",
    )
    # r6 ALLOW list: the venue route still works when no local-host reference appears anywhere in the sentence, across every joiner the
    # r6 rows use. (The exemption is narrowed to "nothing local is named", never removed.)
    CLAUSE_MUST_PASS_R6 = (
        "Run playback locally on Bachelor.",
        "Run playback locally on Bachelor, then read the log.",
        "Run playback locally on Bachelor; read the report afterwards.",
        "Run playback locally on Bachelor; then summarize the result on Ultra-Magnus.",
        "Run playback locally through the venue chain, then post the result to Bachelor.",
        "Run playback locally using Ultra-Magnus and then compare the checksum on Bachelor.",
        "Run playback locally on Bachelor, not on this host; then run the report on Bachelor.",
        "Run playback locally on Bachelor - then verify the checksum through the venue chain.",
        "Run playback on Bachelor rather than locally; then read the log.",
        "Run playback on Bachelor through the venue chain; never locally.",
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
        # r5: a host noun that is a DESTINATION on a venue launcher line (no local marker in play) must not newly refuse the producer
        # prompt shape master and r4 pass.
        f"The venue job calls tools/profiling/{SMOKE_SCRIPT} -Backend cpu on Bachelor and posts the result to the hub.",
        "Run playback on Bachelor through the venue chain, then post the log to the hub.",
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

    def test_red_direct_launch_lines_with_a_second_local_locus_are_refused(self):
        for text in self.CLAUSE_MUST_REFUSE_R7:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_green_clause_scope_venue_routed_wordings_pass(self):
        for text in self.CLAUSE_MUST_PASS + self.CLAUSE_MUST_PASS_R6 + self.CLAUSE_MUST_PASS_R7:
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
                    "HostDeterminers", "HostNouns", "HostNames", "HostPrepositions", "HostLocusPrepositions", "ContrastWords"):
            self.assertTrue(self.lists.get(key), f"list {key} is missing or empty")

    def test_the_lint_builds_its_patterns_from_those_lists(self):
        """The exposed lists must be the lists the lint USES, not a second copy that can drift: the findings function reads them, and the
        literal alternation they replaced is gone."""
        text = LINT_PS1.read_text(encoding="utf-8")
        body = text[text.index("function Get-LocalPlaybackPromptFindings"):]
        self.assertIn("Get-LocalPlaybackLintWordLists", body)
        for key in ("VenueFollowLocus", "VenueFollowPhrases", "HostDeterminers", "HostNouns", "HostNames", "HostPrepositions", "HostLocusPrepositions", "ContrastWords",
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
                  "later-clause-then-host", "host-reference-modifier", "host-reference-two-modifiers", "host-reference-vm-suffix",
                  "host-reference-spelling-variant", "host-name-spelling-variant", "host-reference-bare-preposition",
                  "e1-preposition-possessive", "e3-preposition-then-host", "venue-word-control",
                  "venue-word-then-host", "venue-word-possessive", "contrast-word-then-local-host",
                  "launch-host-locus-det-noun", "launch-host-locus-bare-noun", "launch-host-locus-vm-suffix", "launch-host-locus-other-forms",
                  "launch-host-locus-same-clause", "launch-host-locus-possessive", "launch-host-locus-later-clause",
                  "launch-host-locus-modifier", "launch-host-name", "launch-host-name-bare", "launch-host-spelling-variant",
                  "launch-venue-control", "launch-host-destination-control"):
            self.assertIn(g, groups, f"no rows generated for {g}")
        # preposition x determiner x noun is the full product, not a sample
        product = len(HOST_REF_PREPOSITIONS) * len(self.lists["HostDeterminers"]) * len(self.lists["HostNouns"])
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "host-reference-prep-det-noun"), product)
        # r7: the same on a Rule 4 launcher line -- locus preposition x determiner x noun x venue phrase, every member of the host lists
        launch = len(self.lists["HostLocusPrepositions"]) * len(self.lists["HostDeterminers"]) * len(self.lists["HostNouns"]) * len(LAUNCH_VENUE_PHRASES)
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "launch-host-locus-det-noun"), launch)
        for name in self.lists["HostNames"]:
            self.assertTrue(any(g == "launch-host-name" and name in t for g, _, t in self.rows), f"no Rule 4 row for host name {name}")

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

    def test_red_every_later_clause_with_a_launch_verb_and_a_host_defeats(self):
        """r6, fail closed: whatever the later clause's joiner, verb or (plain) object, it defeats the earlier venue exemption."""
        self._group("later-clause-then-host", True)

    def test_red_every_host_vocabulary_variant_defeats(self):
        for g in ("host-reference-modifier", "host-reference-two-modifiers", "host-reference-vm-suffix", "host-reference-spelling-variant",
                  "host-name-spelling-variant", "host-reference-bare-preposition"):
            self._group(g, True)

    def test_red_every_host_reference_defeats_a_direct_launch_line(self):
        """r7, Rule 4: the same list members, on a direct launcher line whose only evidence of where it runs is the venue phrase.
        A host noun introduced by a locus preposition (on / at / in / using), or a host name, defeats the venue exemption."""
        for g in ("launch-host-locus-det-noun", "launch-host-locus-bare-noun", "launch-host-locus-vm-suffix", "launch-host-locus-other-forms",
                  "launch-host-locus-same-clause", "launch-host-locus-possessive", "launch-host-locus-later-clause",
                  "launch-host-locus-modifier", "launch-host-name", "launch-host-name-bare", "launch-host-spelling-variant"):
            self._group(g, True)

    def test_green_a_destination_on_a_direct_launch_line_still_passes(self):
        """r7 controls: `to | into | from` + determiner + host noun is a destination, not a locus -- every member, every launcher form."""
        self._group("launch-host-destination-control", False)
        self._group("launch-venue-control", False)

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
        ("E1-venue-routed-then-a-clause-that-names-no-local-host",
         lambda text, f: f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_PASS_R6 and text.startswith("Run playback locally "),
         "r6 ALLOW list (CLAUSE_MUST_PASS_R6): the E1 shape above followed by a clause or a semicolon group that names NO local host "
         "('Run playback locally on Bachelor, then read the log.'). Master refuses the `locally` marker in these; the fail-closed "
         "defeater scan reads every later clause and finds nothing local, so the venue route is still a venue route. Their twins that "
         "do name the local host (CLAUSE_MUST_REFUSE r6 block, the per-member later-clause rows) are all refused."),
        ("E2-marker-negated-by-the-contrast-word-before-it",
         lambda text, f: (f.get("tpl") == "venue-contrast-marker" and f.get("M") == "locally")
         or (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_PASS_R6 and text.startswith("Run playback on Bachelor rather than locally")),
         "E2, r1b/r2 (and the r6 ALLOW row 'Run playback on Bachelor rather than locally; then read the log.', the same E2 shape followed by a "
         "semicolon group that names no local host): 'Run playback on Bachelor rather than / instead of / without / nor / , not locally' -- the marker is the first "
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
        ("direct-launch-second-local-locus",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R7)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("launch-host-") and f.get("R") is True),
         "r7, CoS review of #448 a595464f: a direct launcher line (Rule 4) with a SECOND, local locus after the venue ('... -Backend cpu "
         "on Bachelor and on my machine.', '... on Bachelor using this host's GPU.', '... on Bachelor and on localhost.') is refused. "
         "Master's venue exemption is sentence-wide and passes them; the host noun is read only as a LOCUS (on / at / in / using + "
         "determiner + noun) or a host name, so a destination ('... and posts the result to the hub') keeps passing."),
        ("hardening-log-handling-tail",
         lambda text, f: f.get("tpl") == "handling-tail" and f.get("M") in ("on this host", "here"),
         "r1b over-refusal kept on purpose (card HUB-VM-LINT-LOG-COPY-OVERREFUSAL-1): 'copy the log on this host' after a venue run "
         "is refused like its `locally` twin; a handling-verb list would open 'Copy the clip locally and run playback.'"),
    )

    # r7 PREVIOUS-HEAD gate (CoS Prevention on a595464f). Against the lint of the PR head this round started from, NOTHING may open:
    # the ceiling for refuse -> pass is EMPTY, so any flip needs an entry here with a reason AND the same reason in the PR body. A
    # round that narrows an exemption or a defeater (the a595464f Rule 4 narrowing handed back 14 refusals r5 had gained) shows up
    # here even when master passes the row, which is exactly what the master differential cannot see.
    ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS: tuple = ()
    ALLOWED_PREVIOUS_HEAD_PASS_TO_REFUSE = (
        ("r7-direct-launch-second-local-locus",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R7)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("launch-host-") and f.get("R") is True),
         "r7: a host noun after a LOCUS preposition (on / at / in / using), or a host name, defeats the venue exemption on a direct "
         "launcher line (Rule 4). The r6 tip passed 'pwsh -File <launcher> -Backend cpu on Bachelor and on my machine.' and its "
         "relatives; r5 eb3d5f81 refused them. Every row is a Rule 4 line; destinations (to / into / from) stay passing."),
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

    def test_no_refuse_to_pass_flip_against_the_previous_pr_head(self):
        """r7 (CoS Prevention): the same corpus against the lint of the PR head this round started from. Nothing a previous head refused
        may pass on this one without an entry (reason) in ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS -- and the PR body lists it."""
        ref, opened, closed, size = _differential_flips(self.tmp, previous_head=True)
        self.assertEqual(PREVIOUS_PR_HEAD[:8], ref)
        self.assertGreaterEqual(size, 1000, "the corpus shrank; the pin no longer samples the wording space")
        problems = _pin_problems(opened, closed, self.ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS, self.ALLOWED_PREVIOUS_HEAD_PASS_TO_REFUSE)
        self.assertEqual(
            [], problems,
            f"previous PR head {ref}: this round opened or closed a wording the previous head decided the other way -- fix the lint, or "
            f"add an allow-list entry with its reason (and list the flip in the PR body).")
        # every catalogue row that must be refused (this round's and every earlier round's) is refused by the previous head's own
        # verdict OR is one of this round's listed closures; none may be among the opened flips
        flipped = {text for text, _ in opened}
        for text in PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_REFUSE_R7:
            self.assertNotIn(text, flipped, text)

    def test_previous_pr_head_is_an_ancestor_of_head(self):
        """A rebase or a history rewrite that drops PREVIOUS_PR_HEAD must fail here, not silently leave a baseline nobody can reach."""
        _require_previous_head_lint()  # skips (or fails in hosted CI) exactly like the differential does
        done = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", PREVIOUS_PR_HEAD, "HEAD"], capture_output=True, timeout=60)
        self.assertEqual(0, done.returncode, f"PREVIOUS_PR_HEAD {PREVIOUS_PR_HEAD[:8]} is not an ancestor of HEAD; move it to the head this round started from")

    def test_the_previous_head_ceiling_is_empty_or_every_entry_has_a_reason(self):
        for name, _, reason in self.ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS + self.ALLOWED_PREVIOUS_HEAD_PASS_TO_REFUSE:
            self.assertTrue(reason.strip(), name)
        row = ("Run playback locally since Bachelor is busy.", {"tpl": "marker-connective-noun"})
        self.assertTrue(_pin_problems([row], [], self.ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS, self.ALLOWED_PREVIOUS_HEAD_PASS_TO_REFUSE),
                        "an unlisted refuse -> pass flip against the previous head must fail the pin")

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
