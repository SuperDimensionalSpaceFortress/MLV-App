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

import hashlib
import itertools
import json
import os
import random
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
# head the round STARTED from. EACH ROUND MOVES THIS to the head it started from (r13 started from r12 d2f3cd05; r12 started from r11b c574ea89; r11b started from r10
# d291f184; r10 started from r9 fd190b6d, and r9 changed no lint behaviour). It is checked to be an ancestor of HEAD, so a rebase that
# drops it fails loudly in CI instead of silently weakening the gate.
PREVIOUS_PR_HEAD = "d2f3cd05c311f60b8151fe578742c0b3fa104a22"
# NA2-RULE4-PER-LAUNCHER-SCOPE-1: the r1 head of this PR (the lint CoS reproduced the cross-`;` swallow on). The r2 RED rows are
# pinned against it in PromptLintTests (test_red_rule4_r2_*), apart from the previous-head differential above, whose r13 multi-line
# closure assertions are tied to d2f3cd05.
RULE4_R1_PR_HEAD = "75cb00e1213c7282c02f5f53f3908c2e26e4785c"
# r4b (CoS Improvement 6224545625): the master this PR branched from (merge of #448), whose rule 4 read only the first launcher
# occurrence. The RED proof of the r1 second-launcher rows is pinned to it (test_rule4_the_refuse_rows_are_red_on_the_baseline); it
# never moves, because every later master carries the change.
RULE4_PRE_CHANGE_MASTER = "2f8d1a97f954a69e45200fba130e9472e838a178"


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


def _rule4_r1_head_lint(head: str = RULE4_R1_PR_HEAD, label: str = "r1", moot_once_master_carries_it: bool = True) -> tuple[str, str]:
    """(ref, text) of the lint at RULE4_R1_PR_HEAD (or another round head of this PR), with the same skip / fail policy as
    _require_previous_head_lint. `moot_once_master_carries_it=False` is for a check that stays true after the merge (r7 sweep: nothing the
    pre-change master refuses may pass), which must not skip when master already carries this lint."""
    base = _baseline_lint_source()
    if moot_once_master_carries_it and base is not None and base[1].replace("\r\n", "\n") == LINT_PS1.read_text(encoding="utf-8").replace("\r\n", "\n"):
        raise unittest.SkipTest(f"{base[0]} already carries this lint; the {label} head is no longer a baseline")
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"{head}:{LINT_REL}"], capture_output=True, timeout=60)
    if done.returncode == 0 and done.stdout:
        return head[:8], done.stdout.decode("utf-8")
    message = f"{label} PR head {head[:8]} is not in this clone; the RED proof of the next round needs the PR history"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise AssertionError(message + " -- the hygiene jobs check out with fetch-depth: 0, so a missing commit is a broken checkout")
    raise unittest.SkipTest(message)


def _psq(path: object) -> str:
    """A path as a PowerShell single-quoted literal (an apostrophe in the path is doubled, never ends the string)."""
    return "'" + str(path).replace("'", "''") + "'"


def _write_probe(path: Path, body: str) -> None:
    # utf-8-sig, not ascii: a path with a non-ASCII character must reach pwsh intact instead of failing at write time.
    path.write_text(body, encoding="utf-8-sig")


BATCH_SHARDS = 6


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


def _start_json_counts(lint: Path, texts: list[str], work: Path, tag: str) -> "subprocess.Popen":
    """Start ONE pwsh that counts the findings of every text under `lint`, the texts passed as a JSON array so a text may hold line breaks."""
    src = work / f"{tag}-in.json"
    src.write_text(json.dumps(texts), encoding="utf-8")
    probe = work / f"{tag}-probe.ps1"
    _write_probe(
        probe,
        ". " + _psq(lint) + "\n"
        "$texts = @(ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText(" + _psq(src) + ")))\n"
        "$out = New-Object System.Collections.Generic.List[int]\n"
        "foreach ($t in $texts) { $out.Add(@(Get-LocalPlaybackPromptFindings -Text ([string]$t)).Count) }\n"
        "ConvertTo-Json -InputObject @($out.ToArray()) -Compress\n",
    )
    return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _shard_chunks(texts: list[str], rows_per_shard: int) -> list[list[str]]:
    """`texts` cut into up to BATCH_SHARDS chunks of about equal size, one chunk per `rows_per_shard` rows (at least one chunk)."""
    count = max(1, min(BATCH_SHARDS, len(texts) // rows_per_shard))
    size = -(-len(texts) // count) if texts else 0
    return [texts[i:i + size] for i in range(0, len(texts), size)] if size else [texts]


def _start_shards(lint: Path, texts: list[str], work: Path, tag: str, shards: list) -> None:
    """Start the pwsh shards of one lint over `texts` (a text per line), appending (Popen, chunk) to `shards` as each starts (the caller
    owns cleanup). Up to BATCH_SHARDS processes: the member corpus is ~32k rows at ~17 ms a row, which one process cannot finish inside a
    600 s foreground call (r12)."""
    for n, chunk in enumerate(_shard_chunks(texts, 500)):
        shards.append((_lint_finding_counts(lint, chunk, work, f"{tag}-s{n}"), chunk))


# r7 (Iverson I-iv2): the texts that hold a line break go through the JSON array probe, and they are cut into shards the same way as the
# plain ones. Measured at r7: the multi-line catalogue is 34 texts (30 + 4 line-break rows) and the differential corpus holds none, so one
# pwsh takes 0.9 s and the unsharded path was no tail; the split only starts at 200 texts (a shard per 100, a row here is a whole block that
# the head and tail grammars read), so today it changes nothing and a catalogue that grows past that bound no longer queues in one process.
MULTI_ROWS_PER_SHARD = 100


def _start_json_shards(lint: Path, texts: list[str], work: Path, tag: str, shards: list) -> None:
    """Start the pwsh shards of one lint over `texts` that may hold line breaks (JSON array probe), appending (Popen, chunk) to `shards`."""
    for n, chunk in enumerate(_shard_chunks(texts, MULTI_ROWS_PER_SHARD)):
        shards.append((_start_json_counts(lint, chunk, work, f"{tag}-s{n}"), chunk))


# NA2-RULE4-PER-LAUNCHER-SCOPE-1 r3: the finding count of a text is a pure function of (the lint's bytes, the text), so it is computed at
# most once per module run. The catalogue tests, the list-member setUp, the differential pins and the r1-head RED proof all ask for the
# same (lint, text) pairs; before r3 each asked its own pwsh (the candidate lint alone ran the whole ~34k-row corpus three times).
# The cache relies on Get-LocalPlaybackPromptFindings staying location- and env-independent: its verdict may depend only on the lint's
# bytes and the text, never on the working directory, an environment variable, the clock or the host. A lint change that reads any of
# those must key the cache on it too (or drop the cache).
_VERDICTS: dict[tuple[str, str], int] = {}


def _lint_digest(lint: Path) -> str:
    """Identity of a lint copy: its bytes with CRLF folded to LF (the same equivalence the 'master already carries this lint' checks use)."""
    return hashlib.sha256(Path(lint).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _prefetch_counts(work: Path, jobs: list[tuple[Path, list[str], str]]) -> None:
    """Fill _VERDICTS for every (lint, texts, tag) job, running only the (lint bytes, text) pairs not computed yet. Every job's pwsh
    processes run at the same time and are joined here. A text without a line break goes through the line-per-text shards; a text
    with one (or an empty text) goes through the JSON array probe."""
    pending: list[tuple[str, list]] = []  # (lint digest, [(Popen, texts)])
    queued: set[tuple[str, str]] = set()
    try:
        for lint, texts, tag in jobs:
            digest = _lint_digest(lint)
            missing = [t for t in dict.fromkeys(texts) if (digest, t) not in _VERDICTS and (digest, t) not in queued]
            queued.update((digest, t) for t in missing)
            plain = [t for t in missing if t and "\n" not in t and "\r" not in t]
            multi = [t for t in missing if not (t and "\n" not in t and "\r" not in t)]
            if plain:
                shards: list = []
                pending.append((digest, shards))
                _start_shards(lint, plain, work, f"{tag}-p", shards)
            if multi:
                procs: list = []
                pending.append((digest, procs))
                _start_json_shards(lint, multi, work, f"{tag}-m", procs)
        for digest, shards in pending:
            for proc, chunk in shards:
                for text, count in zip(chunk, _collect_counts(proc, len(chunk))):
                    _VERDICTS[(digest, text)] = count
    finally:
        for _, shards in pending:
            for proc, _ in shards:
                _kill(proc)


def _verdicts(lint: Path, texts: list[str]) -> list[int]:
    """The finding count of every text under `lint`; _prefetch_counts must have run for it."""
    digest = _lint_digest(lint)
    return [_VERDICTS[(digest, t)] for t in texts]


def _json_counts(lint: Path, texts: list[str], work: Path, tag: str) -> list[int]:
    """Finding count of every text under `lint`; a text may hold line breaks. Computed once per (lint bytes, text) per module run."""
    _prefetch_counts(work, [(lint, texts, tag)])
    return _verdicts(lint, texts)


def _batch_counts(lint: Path, texts: list[str], work: Path, tag: str) -> list[int]:
    """Finding count of every text under `lint`, sharded over up to BATCH_SHARDS pwsh processes (see _start_shards)."""
    return _json_counts(lint, texts, work, tag)


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
        # r10: the corpus grew past 26k rows and the baseline lints take several ms a row on a loaded runner; r12: ~32k rows at ~19 ms a
        # row on a loaded host measured 600 s and timed out, so the bound is 1500 s
        out, err = proc.communicate(timeout=1500)
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


# r8 axes (CoS review of PR #448 r6 head 4957fff0, Blocker 2, 6th round; pr-448.md on bus c014c93f). r8 makes E1 sentence-terminal: after
# `locally <prep> <venue>` the rest of the sentence AND of the instruction block may only be more venue routing, a pure locus word, a
# negation that governs a host reference, or a handling clause (a handling verb and its object: no launch verb, no locus, no host
# reference). Anything else voids the exemption. The generators below do NOT draw from the lint's word lists: a refusal that needed a word
# added to a lint list would mean the axis failed its purpose, so every pool is test-only and asserted DISJOINT from the lint's lists.
HELD_OUT_HOST_NOUNS = (
    "rig", "instance", "container", "sandbox", "session", "install", "hypervisor", "terminal", "console", "tower", "notebook", "mainframe",
    "droplet", "appliance", "kiosk", "cluster", "blade", "enclave", "jail", "shell", "workbench", "dev rig", "build agent", "test bench",
    "Windows install", "VMware image", "WSL distro", "docker image", "cloud instance", "gaming rig",
)
HELD_OUT_DETERMINERS = ("this", "these", "that", "the", "my", "our", "your", "his", "her", "each", "every", "another", "the current", "this very")
HELD_OUT_PREPOSITIONS = ("on", "in", "at", "inside", "within", "onto", "using", "via", "through", "under", "atop", "over")
HELD_OUT_LAUNCH_VERBS = ("run", "play", "open", "start", "launch", "execute", "rerun", "replay", "repeat", "relaunch", "restart", "redo", "retry",
                         "perform", "trigger", "spawn", "boot", "render", "fire up", "kick off", "try", "test", "use", "do", "exercise")
R8_LATER_JOINERS = (", then", ", and then", "; then", "; ", " and", ", ", " - then", ", so", ", but")
R8_LATER_OBJECTS = ("the clip", "it", "the build", "MLVApp", "playback", "the arms", "this")
R8_LATER_TAILS = ("here", "on my machine", "locally", "on the hub-VM", "on this host", "on this rig", "in this container", "")
R8_NEGATION_TAILS = (" with no overlay", ", no audio", " if not busy", ", no need to wait", ", not on Ultra-Magnus", " unless it is busy", " without audio")
R8_SENTENCE_JOINERS = (". Then", ". Also", ". Next", ". After that", ". Afterwards,", ". And", ". Finally,", ".")
R8_BASE = "Run playback locally on Bachelor"


def _r8_rows(lists: dict[str, list[str]]) -> list[tuple[str, bool, str]]:
    """(group, must_refuse, text): the r8 axes -- held-out vocabulary, negation tail, cross-sentence, and the members of the r8 lists."""
    out: dict[str, tuple[str, bool]] = {}

    def add(group: str, refuse: bool, text: str) -> None:
        out.setdefault(text, (group, refuse))

    # (1) held-out vocabulary: a host noun, determiner and preposition the lint does not list, verbless and behind a verb
    for j in (" and", ", then", "; then", ". Then"):
        for prep in HELD_OUT_PREPOSITIONS[:6]:
            for det in HELD_OUT_DETERMINERS[:6] + HELD_OUT_DETERMINERS[-2:]:
                for noun in HELD_OUT_HOST_NOUNS:
                    add("r8-held-out-verbless", True, f"{R8_BASE}{j} {prep} {det} {noun}.")
    for j in R8_LATER_JOINERS[:5]:
        for verb in HELD_OUT_LAUNCH_VERBS:
            for noun in HELD_OUT_HOST_NOUNS[:6]:
                add("r8-held-out-verb-noun", True, f"{R8_BASE}{j} {verb} it on this {noun}.")
                add("r8-held-out-verb-noun", True, f"{R8_BASE}{j} {verb} the clip in the {noun}.")
    for noun in HELD_OUT_HOST_NOUNS:
        add("r8-held-out-trailing", True, f"{R8_BASE} and on this {noun} too.")
        add("r8-held-out-trailing", True, f"{R8_BASE}; play the clip on the same {noun}.")
    # (2) negation / condition tail: every later-clause launch row repeated with a trailing qualifier
    for j in R8_LATER_JOINERS[:5]:
        for verb in ("run", "play", "open", "rerun"):
            for obj in R8_LATER_OBJECTS[:4]:
                for tail in R8_LATER_TAILS[:5]:
                    for neg in R8_NEGATION_TAILS:
                        add("r8-negation-tail", True, f"{R8_BASE}{j} {verb} {obj}{(' ' + tail) if tail else ''}{neg}.")
    # (3) cross-sentence: the same later clause as a second sentence after a period
    for j in R8_SENTENCE_JOINERS:
        for verb in ("run", "play", "open", "rerun"):
            for obj in R8_LATER_OBJECTS[:4]:
                for tail in R8_LATER_TAILS:
                    add("r8-cross-sentence", True, f"{R8_BASE}{j} {verb} {obj}{(' ' + tail) if tail else ''}.")
    for noun in HELD_OUT_HOST_NOUNS:
        add("r8-cross-sentence", True, f"{R8_BASE}. Then also run it on this {noun}.")
        add("r8-cross-sentence", True, f"{R8_BASE}. And on this {noun}.")
    # (4) r8's handling verbs (the r8/r9 lint list, a test-only literal since r10 removed it): r10 refuses the plain form too (accepted
    # over-refusal, ruling 20261015T1650Z), and every form with a locus, a host reference or a launch verb stays refused
    for v in R10_FORMER_HANDLING_VERBS:
        add("r10-overrefusal-handling", True, f"{R8_BASE}, then {v} the log.")
        add("r10-overrefusal-handling", True, f"{R8_BASE}. {v.capitalize()} the log.")
        add("r8-handling-then-host", True, f"{R8_BASE}, then {v} the log here.")
        add("r8-handling-then-host", True, f"{R8_BASE}, then {v} the log on this rig.")
        add("r8-handling-then-host", True, f"{R8_BASE}. {v.capitalize()} the log on this host.")
        add("r8-handling-then-launch", True, f"{R8_BASE}, then {v} the log and play it.")
        add("r8-handling-then-launch", True, f"{R8_BASE}, then {v} the log, replay it.")
    for p in lists.get("TailLocusPrepositions", []):
        add("r8-locus-preposition-then-unknown", True, f"{R8_BASE}, then read the log {p} this rig.")
        add("r8-locus-preposition-then-unknown", True, f"{R8_BASE} and {p} this rig.")
    # (a comma directly after the venue name is a closed follow list since r5 -- ", no here" never reached E1 -- so the members are
    # written as a semicolon group and as a following sentence, the two forms that do reach the tail)
    for n in lists.get("TailNegations", []):
        add("r8-negation-governs-host-control", False, f"{R8_BASE}; {n} here.")
        add("r8-negation-governs-host-control", False, f"{R8_BASE}; {n} on this host.")
        add("r8-negation-governs-host-control", False, f"{R8_BASE}. {n.capitalize()} here.")
        add("r8-negation-then-launch", True, f"{R8_BASE}; {n} here, then run it on this rig.")
        add("r8-negation-then-launch", True, f"{R8_BASE}; {n} on Ultra-Magnus, then run it here.")
        add("r8-negation-then-launch", True, f"{R8_BASE}. {n.capitalize()} here. Then run it on this rig.")
    for w in lists.get("TailPureLocus", []):
        add("r8-pure-locus-control", False, f"{R8_BASE}; {w}.")
        add("r8-pure-locus-then-launch", True, f"{R8_BASE}; {w}, then run it here.")
        add("r8-pure-locus-then-launch", True, f"{R8_BASE}. {w.capitalize()}. Then run it here.")
    # (5) venue routing keeps passing: more venue routing, every venue word, every routing preposition, across the joiners
    for w in lists.get("VenueWords", []):
        for j in (" and then", ", then", "; then", ", and"):
            add("r8-venue-routing-control", False, f"{R8_BASE}{j} on {w}.")
    for p in lists.get("RoutingPrepositionsVenue", []):
        add("r8-venue-routing-control", False, f"{R8_BASE} {p} the venue chain.")
        add("r10-overrefusal-handling", True, f"{R8_BASE}, then summarize the result {p} Ultra-Magnus.")
    return [(g, r, t) for t, (g, r) in out.items()]


# r10 axes (hub ruling HUB-TICK 20261015T1650Z, STRICTLY TERMINAL E1; r9 keys sol + opus on fd190b6d). The tail after the venue phrase
# must match a CLOSED, VERBLESS allow-grammar: (A) locus preposition + venue, (B) a pure locus word, (C) a verbless host negation. The
# refusal generators below draw from TEST-ONLY pools (asserted disjoint from the lint's tail lists), never from the lint's lists.
R10_FORMER_HANDLING_VERBS = ("read", "summarize", "summarise", "compare", "verify", "check", "report", "post", "write", "record", "log", "list",
                             "collect", "gather", "fetch", "attach", "copy", "save", "store", "inspect", "review", "diff", "hash", "parse",
                             "extract", "upload", "publish", "note", "print", "append", "update", "commit")
R10_ADVERBS = ("quietly", "smoothly", "briefly", "carefully", "silently", "manually")
R10_LEAD_LAUNCH_VERBS = ("play", "replay", "launch")
# the lead shapes of the r9 keys: a handling clause that carries the launch verb further in
R10_EMBEDDING_LEADS = ("record yourself", "verify this tablet can", "check that MLVApp can", "copy the clip so the GUI can", "confirm the rig can")
R10_DESTINATION_PREPOSITIONS = ("to", "into", "from", "for")
R10_DESTINATION_OBJECTS = ("it", "the clip")
R10_VENUE_POOL = ("Bachelor", "the venue chain", "Ultra-Magnus", "the venue job")
# the ruling's grammar, as literal test pools for the ALLOW generator (an independent statement of what must keep passing)
R10_ALLOW_PREPOSITIONS = ("on", "via", "through", "using", "at")
R10_ALLOW_JOINERS = (", then", " and", " or", ", and then", "; then", " then", ", or", " - then", ". Then", ".")
R10_ALLOW_LOCUS = ("from here", "afterwards", "afterward", "later", "finally", "next", "too", "as well")
R10_ALLOW_NEGATIONS = ("not", "never", "nor")
R10_ALLOW_HOST_TARGETS = ("here", "locally", "on this host", "on the hub", "at this machine", "on localhost", "on my own box", "on hub")


def _r10_rows(lists: dict[str, list[str]]) -> list[tuple[str, bool, str]]:
    """(group, must_refuse, text): the r10 axes -- adverb insertion, destination prepositions after a launch lead, both split across a
    sentence, held-out host nouns; the members of the r10 tail lists; and the ALLOW rows of the closed grammar."""
    out: dict[str, tuple[str, bool]] = {}

    def add(group: str, refuse: bool, text: str) -> None:
        out.setdefault(text, (group, refuse))

    # (1) adverb insertion between every lead the r9 tail admitted (a handling verb and its object, the r9 keys' embedding leads) and a
    # launch verb; (3) the same as a second sentence
    for hv in R10_FORMER_HANDLING_VERBS:
        for adv in R10_ADVERBS:
            for verb in R10_LEAD_LAUNCH_VERBS:
                add("r10-adverb-insertion", True, f"{R8_BASE}, then {hv} the log to {adv} {verb} it.")
                add("r10-adverb-insertion-cross-sentence", True, f"{R8_BASE}. Then {hv} the log to {adv} {verb} it.")
    for lead in R10_EMBEDDING_LEADS:
        for adv in R10_ADVERBS[:3]:
            for verb in HELD_OUT_LAUNCH_VERBS:
                add("r10-adverb-insertion", True, f"{R8_BASE}, then {lead} {adv} {verb} it.")
                add("r10-adverb-insertion-cross-sentence", True, f"{R8_BASE}. Then {lead} {adv} {verb} it.")
    # (2) every destination preposition after a launch lead; (3) the same as a second sentence
    for verb in HELD_OUT_LAUNCH_VERBS:
        for obj in R10_DESTINATION_OBJECTS:
            for prep in R10_DESTINATION_PREPOSITIONS:
                for venue in R10_VENUE_POOL:
                    add("r10-destination-preposition", True, f"{R8_BASE}, then {verb} {obj} {prep} {venue}.")
                    add("r10-destination-preposition-cross-sentence", True, f"{R8_BASE}. Then {verb} {obj} {prep} {venue}.")
    # (4) held-out host nouns: in a host negation (C admits listed host nouns only), behind an adverb-separated launch, verbless
    for noun in HELD_OUT_HOST_NOUNS:
        add("r10-held-out-host", True, f"{R8_BASE}, not on this {noun}.")
        add("r10-held-out-host", True, f"{R8_BASE}, then verify this {noun} can smoothly play it.")
        add("r10-held-out-host", True, f"{R8_BASE} and on this {noun}.")
        add("r10-held-out-host", True, f"{R8_BASE}. Then on this {noun}.")
    # (5) one control (passes) and defeaters (refuse) per member of every r10 tail list, read from the lint
    for p in lists.get("TailLocusPrepositions", []):
        add("r10-member-control", False, f"{R8_BASE}, then {p} Ultra-Magnus.")
        add("r10-member-defeat", True, f"{R8_BASE}, then {p} this rig.")
        add("r10-member-defeat", True, f"{R8_BASE}, then {p} Ultra-Magnus and play it.")
    for a in lists.get("TailArticles", []):
        add("r10-member-control", False, f"{R8_BASE}, then on {a} venue job.")
        add("r10-member-defeat", True, f"{R8_BASE}, then on {a} rig.")
    for j in lists.get("TailJoiners", []):
        add("r10-member-control", False, f"{R8_BASE} {j} on Ultra-Magnus.")
        add("r10-member-defeat", True, f"{R8_BASE} {j} play it on Ultra-Magnus.")
    for n in lists.get("TailNegations", []):
        add("r10-member-control", False, f"{R8_BASE}, {n} here.")
        add("r10-member-defeat", True, f"{R8_BASE}, {n} here, then run it.")
        add("r10-member-defeat", True, f"{R8_BASE}, {n} on this rig.")
    for p in lists.get("TailNegationPrepositions", []):
        add("r10-member-control", False, f"{R8_BASE}, not {p} this host.")
        add("r10-member-defeat", True, f"{R8_BASE}, not {p} this host, play it here.")
    for w in lists.get("TailPureLocus", []):
        add("r10-member-control", False, f"{R8_BASE}; {w}.")
        add("r10-member-defeat", True, f"{R8_BASE}; {w}, then run it here.")
        add("r10-member-defeat", True, f"{R8_BASE}; {w} quietly.")
    for w in lists.get("VenueWords", []):
        add("r10-member-control", False, f"{R8_BASE}, then on {w}.")
        add("r10-member-defeat", True, f"{R8_BASE}, then play it from {w}.")
    # (6) ALLOW: tails in the grammar only, from the literal pools above
    for j in R10_ALLOW_JOINERS:
        for prep in R10_ALLOW_PREPOSITIONS:
            for venue in R10_VENUE_POOL:
                tail = f" {prep} {venue}." if j != "." else f" {prep[0].upper()}{prep[1:]} {venue}."
                add("r10-grammar-allow", False, f"{R8_BASE}{j}{tail}")
    for w in R10_ALLOW_LOCUS:
        add("r10-grammar-allow", False, f"{R8_BASE}; {w}.")
        add("r10-grammar-allow", False, f"{R8_BASE} through the venue chain {w}.")
    for n in R10_ALLOW_NEGATIONS:
        for target in R10_ALLOW_HOST_TARGETS:
            add("r10-grammar-allow", False, f"{R8_BASE}, {n} {target}.")
            add("r10-grammar-allow", False, f"{R8_BASE}. {n.capitalize()} {target}.")
    return [(g, r, t) for t, (g, r) in out.items()]


# r11b axes (opus r10 key on d291f184: the HEAD of an E1 sentence was never read). E1 needs every earlier clause, every earlier ';' group,
# the marker clause's own prefix and every earlier item of the block to be benign. The refusal generators draw their VERBS and NOUNS from
# the r8 held-out pools (the lint lists neither), so a refusal rests on the head rule and not on a listed word. The marker clause carries
# a verb the lint does list ("run playback locally ...") so that master refuses the row too: a head-only row would never be a finding.
R11_BOUNDARIES = (" and", ", then", ", and then", "; then", ", but", ", so", " - then", ", and", ";")
R11_KNOWN_VERBS = ("Run", "Play", "Open", "Start", "Launch", "Execute")
R11_LOCI = ("on the hub-VM", "on my machine", "on this host", "on my rig", "in this container", "at this workstation")
R11_WEAK_NEGATIONS = ("with no overlay", "no audio", "without audio")
R11_SENTENCE_BOUNDARIES = (". Then run", ". Run", ". Also run", ". Afterwards, run")
R11_PREFIX_PREPOSITIONS = ("on", "in", "at", "inside", "within", "onto", "using", "via", "through", "under", "atop", "over")
R11_ALLOW_ROUTES = ("on Bachelor", "via the venue chain", "through the venue chain", "using Ultra-Magnus", "at the venue job")


def _r11_rows(lists: dict[str, list[str]]) -> list[tuple[str, bool, str]]:
    """(group, must_refuse, text): the r11b axes -- an earlier launch in front of a venue-routed `locally`, across every clause boundary,
    sentence boundary, host locus, weak negation and r10 allow tail; a host in the marker clause's own prefix; and the ALLOW controls."""
    out: dict[str, tuple[str, bool]] = {}

    def add(group: str, refuse: bool, text: str) -> None:
        out.setdefault(text, (group, refuse))

    marker = "run playback locally on Bachelor"
    # (1) held-out launch verb x held-out host noun x every clause boundary, then the marker clause
    for verb in HELD_OUT_LAUNCH_VERBS:
        for noun in HELD_OUT_HOST_NOUNS[:6]:
            for j in R11_BOUNDARIES:
                add("r11-earlier-clause-held-out", True, f"{verb.capitalize()} the arms on my {noun}{j} {marker}.")
    # (2) the lint's own launch verbs x every locus x every boundary, with and without a verb in the marker clause
    for verb in R11_KNOWN_VERBS:
        for locus in R11_LOCI:
            for j in R11_BOUNDARIES:
                # a `;` cuts the marker clause away from the playback test when it holds no verb of its own (group scope, as at master
                # and at every earlier head), so the verbless form is only generated for the boundaries that keep one group
                if ";" not in j:
                    add("r11-earlier-clause-known", True, f"{verb} playback {locus}{j} locally on Bachelor.")
                add("r11-earlier-clause-known", True, f"{verb} the arms {locus}{j} {marker}.")
    # (3) the same earlier launch as an earlier SENTENCE of the block
    for verb in R11_KNOWN_VERBS:
        for locus in R11_LOCI:
            for j in R11_SENTENCE_BOUNDARIES:
                add("r11-earlier-sentence", True, f"{verb} the arms {locus}{j} playback locally on Bachelor.")
    # (4) a weak negation in the head never exempts it (the strong ones do)
    for neg in R11_WEAK_NEGATIONS:
        for locus in R11_LOCI:
            for j in R11_BOUNDARIES[:5]:
                add("r11-weak-negation-head", True, f"Run playback {locus} {neg}{j} {marker}.")
    # (5) a launch in an earlier semicolon group
    for locus in R11_LOCI:
        add("r11-earlier-group", True, f"Run playback {locus}; {marker}.")
        add("r11-earlier-group", True, f"Run the arms {locus}; then {marker}.")
    # (6) the r10 allow tails behind an earlier launch: the tail is in the grammar, the head is not benign
    for verb in HELD_OUT_LAUNCH_VERBS[:8]:
        for noun in HELD_OUT_HOST_NOUNS[:4]:
            for prep in R10_ALLOW_PREPOSITIONS[:3]:
                for venue in R10_VENUE_POOL[:2]:
                    add("r11-earlier-clause-allow-tail", True, f"{verb.capitalize()} the arms on this {noun}, then {marker} {prep} {venue}.")
    # (7) a host in the marker clause's own prefix, whatever the preposition and the noun
    for prep in R11_PREFIX_PREPOSITIONS:
        for noun in HELD_OUT_HOST_NOUNS[:10]:
            add("r11-marker-prefix-host", True, f"Run playback {prep} my {noun} locally on Bachelor.")
    # (8) an earlier un-routed launch with no host at all (accepted over-refusal)
    for verb in ("run", "start", "open"):
        for obj in ("the build", "the tests", "the app"):
            for j in R11_BOUNDARIES[:4]:
                add("r11-overrefusal-earlier-launch", True, f"{verb.capitalize()} {obj}{j} {marker}.")
    # (9) ALLOW: the head is itself venue-routed with no host, across the same boundaries and routes
    for route in R11_ALLOW_ROUTES:
        for j in R11_BOUNDARIES[:5]:
            add("r11-allow-routed-head", False, f"Run playback {route}{j} locally on Bachelor.")
            add("r11-allow-routed-head", False, f"Run the arms {route}{j} {marker}.")
        for j in R11_SENTENCE_BOUNDARIES[:2]:
            add("r11-allow-routed-head", False, f"Run the arms {route}{j} playback locally on Bachelor.")
    return [(g, r, t) for t, (g, r) in out.items()]


# r12 axes (opus + sol r11b keys on c574ea89: an earlier sentence or clause whose launch verb the lint does not list, or whose boundary
# hid the verb, escaped the r11b head veto). The head is a CLOSED whitelist now: a verb that is not one of the lint's listed launch
# verbs is not a token, so "Re-run" / "Replay" / "Preview" voids E1 whatever host it names, or none. Every pool below is TEST-ONLY and is
# asserted disjoint from the lint's lists (test_r12_held_out_pools_are_disjoint_from_the_head_lists). The marker clause carries a verb
# the lint DOES list, so master refuses each row as well (a head-only row would never be a finding).
R12_UNLISTED_VERBS = ("replay", "re-run", "rerun", "repeat", "preview", "reproduce", "rehearse", "perform", "exercise", "trigger", "spawn",
                      "fire up", "boot", "render", "stream", "demo", "view", "watch", "scrub", "stress", "soak", "relaunch", "restart",
                      "redo", "retry", "try", "bring up", "spin up", "cycle", "emulate", "simulate", "do", "use", "test")
R12_VERB_FORMS = ("re-{v}", "re{v}", "pre-{v}", "{v}-again", "{v}-through", "auto-{v}")
# The full product is bounded for run time (the class probes every row twice, ~19 ms a row on a loaded host): six loci in the full
# unlisted-verb product, the rarer loci only against the first boundary of every verb in R12_EXTRA_LOCI.
R12_LOCI = ("on this host", "on the hub-VM", "here", "on my rig", "on Bachelor", "")
R12_EXTRA_LOCI = ("on localhost", "on this tablet", "with this local install", "hub-side", "on-box", "in this container")
R12_FILLER_POOL = ("please", "just", "also", "now")
# r13: the file arguments behind an executable. Deliberately neutral made-up names (hub ruling 2026-10-16: a fixture never names a real
# clip file or uses the raw clip extension): the rule under test is that ANY file argument after an executable is a launch.
R13_ARGS = ("", " take01.dat", " sample1.bin", " C1")
# A file argument the closed grammar does not read as an identifier (no digit): refused by every head, so it is a refuse-only control.
R13_UNLISTED_ARGS = (" sample-a.bin",)


def _r12_rows(lists: dict[str, list[str]]) -> list[tuple[str, bool, str]]:
    """(group, must_refuse, text): the r12 axes -- an unlisted verb, and every hyphenated / prefixed form of every listed verb, x every
    locus x every clause and sentence boundary; an unlisted verb that is venue-routed (accepted over-refusal); the ALLOW controls."""
    out: dict[str, tuple[str, bool]] = {}

    def add(group: str, refuse: bool, text: str) -> None:
        out.setdefault(text, (group, refuse))

    marker = "run playback locally on Bachelor"

    def head_of(verb: str, locus: str) -> str:
        return f"{verb.capitalize()} the arms" + (f" {locus}" if locus else "")

    # (1) an unlisted verb x locus (a listed host, an unlisted host, none, a venue) x every clause boundary and sentence boundary
    for verb in R12_UNLISTED_VERBS:
        for locus in R12_LOCI:
            head = head_of(verb, locus)
            for j in R11_BOUNDARIES:
                add("r12-unlisted-verb-clause" if locus != "on Bachelor" else "r12-overrefusal-unlisted-verb-routed", True, f"{head}{j} {marker}.")
            for j in R11_SENTENCE_BOUNDARIES:
                add("r12-unlisted-verb-sentence" if locus != "on Bachelor" else "r12-overrefusal-unlisted-verb-routed", True,
                    f"{head}{j} playback locally on Bachelor.")
    for verb in R12_UNLISTED_VERBS:
        for locus in R12_EXTRA_LOCI:
            add("r12-unlisted-verb-clause", True, f"{head_of(verb, locus)}{R11_BOUNDARIES[0]} {marker}.")
    # (2) the hyphenated / prefixed form of EVERY listed launch verb: the word boundary of the old head veto hid `run` in `re-run`
    for verb in lists["LaunchVerbs"]:
        for fmt in R12_VERB_FORMS:
            form = fmt.format(v=verb.replace(" ", "-"))
            for locus in R12_LOCI[:3]:
                head = head_of(form, locus)
                for j in R11_BOUNDARIES[:3]:
                    add("r12-hyphen-prefix-form", True, f"{head}{j} {marker}.")
                add("r12-hyphen-prefix-form", True, f"{head}. Then {marker}.")
    # (3) a launch with no locus at all, then an unrelated sentence, then the venue-routed `locally`: the head reads EVERY earlier item
    for verb in R12_UNLISTED_VERBS[:12]:
        add("r12-earlier-item-no-locus", True, f"{verb.capitalize()} the arms. Check the log. Then {marker}.")
        add("r12-earlier-item-no-locus", True, f"Check the log. {verb.capitalize()} the arms on this host. Then {marker}.")
    # (4) ALLOW: the closed head grammar's own shapes -- fillers, identifiers, prohibitions of a host, contrast, every joiner
    for filler in lists["HeadFillers"]:
        add("r12-allow-head", False, f"{filler.capitalize()} {marker}.")
        add("r12-allow-head", False, f"Run playback on Bachelor, then {filler} {marker}.")
    for ident in ("M16-1243", "arm2", "C1", "v1.2"):
        add("r12-allow-head", False, f"Run the {ident} clip locally on Bachelor.")
        add("r12-allow-head", False, f"Run the {ident} arms on Bachelor and locally on Bachelor.")
    for neg in lists["HeadProhibitions"]:
        for target in ("here", "locally", "on this host", "on the hub", "on localhost", "at this machine"):
            add("r12-allow-head", False, f"{neg.capitalize()} run playback {target}, {marker}.")
            add("r12-allow-head", False, f"{neg.capitalize()} run playback {target}. Run playback locally on Bachelor.")
    for contrast in lists["HeadContrasts"]:
        add("r12-allow-head", False, f"Run playback on Bachelor {contrast} here, then {marker}.")
    for joiner in lists["HeadJoiners"]:
        add("r12-allow-head", False, f"Run playback on Bachelor, {joiner} locally on Bachelor.")
    # (5) r13 (sol r12 BLOCKER HUB-VM-LINT-HEAD-EXECUTABLE-AS-OBJECT-1): an executable, or a clip / file argument, standing alone is a
    # LAUNCH with no route, never an object phrase. Every executable x argument x every clause and sentence boundary refuses.
    for exe in lists["HeadExecutableWords"]:
        for arg in R13_ARGS:
            for j in R11_BOUNDARIES:
                add("r13-executable-as-object", True, f"{exe}{arg}{j} {marker}.")
            for j in R11_SENTENCE_BOUNDARIES:
                add("r13-executable-as-object", True, f"{exe}{arg}{j} playback locally on Bachelor.")
            # ALLOW controls: the same launch carrying its OWN route, after a prohibition, or as the marker's own launch
            add("r13-allow-routed-executable", False, f"Run {exe}{arg} on Bachelor, then locally on Bachelor.")
            add("r13-allow-routed-executable", False, f"{exe}{arg} on Bachelor; {marker}.")
            add("r13-allow-routed-executable", False, f"Never run {exe}{arg}, {marker}.")
            add("r13-allow-routed-executable", False, f"Run {exe}{arg} locally on Bachelor.")
    for exe in lists["HeadExecutableWords"]:
        for arg in R13_UNLISTED_ARGS:
            for j in R11_BOUNDARIES:
                add("r13-executable-as-object", True, f"{exe}{arg}{j} {marker}.")
    for arg in R13_ARGS[1:]:
        for j in R11_BOUNDARIES:
            add("r13-executable-as-object", True, f"{arg.strip()}{j} {marker}.")
    return [(g, r, t) for t, (g, r) in out.items()]


# The ruling's grammar restated in Python, independent of the lint: the master differential may let a `locally` row flip refuse -> pass
# only when its venue phrase is terminal under it (ruling 20261015T1650Z: "no master refusal may flip to pass unless its venue phrase
# is terminal under the grammar"). Deliberately a little NARROWER than the lint where that is simpler: a flip it cannot explain fails.
_G_TB = r"(?![\w\-'])"
_G_VENUE = (r"(?:invoke-venuechain\.ps1|invoke-venueleg\.ps1|invoke-venuechain|invoke-venueleg|ultra-magnus|chain\s+runner|bachelor|um-run|venues|venue)"
            + _G_TB + r"(?:\s+(?:chain|chains|runner|job|jobs|leg|legs|lane)" + _G_TB + r")*")
_G_ROUTE = r"(?:on|via|through|using|at)" + _G_TB + r"\s+(?:(?:the|a|an)" + _G_TB + r"\s+)?" + _G_VENUE
_G_LOCUS = r"(?:from\s+here|afterwards|afterward|later|finally|next|too|as\s+well)" + _G_TB
_G_HOST_DET = r"(?:this|the|my|our|your|that|local|current|same|own)"
_G_HOST_NOUN = r"(?:hub[\s\-]*vm|hub|host|vm|machine|box|pc|server)(?!\w)"
_G_HOST_TARGET = (r"(?:here" + _G_TB + r"|locally" + _G_TB + r"|(?:(?:on|at)\s+)?localhost(?!\w)|(?:on|at)\s+"
                  + _G_HOST_NOUN + r"|(?:(?:on|at)\s+)?" + _G_HOST_DET + r"(?:\s+" + _G_HOST_DET + r"){0,2}\s+" + _G_HOST_NOUN + r")")
_G_NEGATION = r"(?:not|never|nor)" + _G_TB + r"\s+" + _G_HOST_TARGET
_G_FRAGMENT = r"(?:(?:" + _G_LOCUS + r"\s+)*" + _G_ROUTE + r"(?:\s+" + _G_ROUTE + r")*(?:\s+" + _G_LOCUS + r")*|" + _G_NEGATION + r"|" + _G_LOCUS + r")"
_G_SEPARATOR = r"(?:[.!?;:,]+(?=\s|$)|(?<=\s)-{1,2}(?=\s)|(?:and|or|then)" + _G_TB + r")"
_G_TAIL = re.compile(r"\s*(?:" + _G_SEPARATOR + r"\s*)*(?:" + _G_FRAGMENT + r"(?:\s*" + _G_SEPARATOR + r")+\s*)*(?:" + _G_FRAGMENT + r"\s*)?", re.I)
_G_E1 = re.compile(r"^(?P<head>.*?)\blocally\s+(?:on|via|through|using)\s+(?:(?:the|a|an)\s+)?" + _G_VENUE, re.I)


# r12: the HEAD of the marker (everything before `locally`, earlier sentences included), restated as the same closed whitelist the lint
# builds, independent of it (its own word lists; a verb the grammar does not name is not a token). A fragment is exactly one of:
# a prohibition (never / do not ... + an optional launch verb + objects + host targets or routes), a verbless host negation, a
# venue-routed launch ([fillers] [verb] [objects] route+ [contrast host-target]), a pure locus word, a bare venue name or an object
# phrase; the LAST piece may also be a bare launch. Deliberately no wider than the lint: a flip against master it cannot explain fails.
_G_VERB = (r"(?:running|runs|run|launching|launched|launches|launch|starting|starts|start|executing|executes|execute|invoking|invokes|invoke|"
           r"driving|drives|drive|kick\s+off|playing|plays|play|opening|opens|open)" + _G_TB)
_G_OBJ_WORD = r"(?:playback|arms?|clips?|it|them|this|that|app|exactness|gui-smoke|gui|smoke)" + _G_TB
_G_EXE = r"(?:mlvapprelease(?:\.exe)?|mlvapp(?:\.exe)?)" + _G_TB
_G_IDENT = r"[A-Za-z]{1,6}\d+(?:[\-.]\w+)*" + _G_TB
# r13: an executable or a clip / file argument is a LAUNCH, never a stand-alone object phrase (_G_OBJECT, plain words only); it is read
# (_G_LAUNCH_OBJECT) only where the fragment carries its own route, after a prohibition word or as the marker's own bare launch.
_G_OBJECT = r"(?:(?:the|a|an)" + _G_TB + r"\s+)?" + _G_OBJ_WORD + r"(?:\s+" + _G_OBJ_WORD + r")*"
_G_LAUNCH_OBJECT = (r"(?:(?:the|a|an)" + _G_TB + r"\s+)?(?:" + _G_OBJ_WORD + r"|" + _G_EXE + r"|" + _G_IDENT + r")(?:\s+(?:" + _G_OBJ_WORD + r"|"
                    + _G_EXE + r"|" + _G_IDENT + r"))*")
_G_FILLERS = r"(?:(?:please|just|also|now)" + _G_TB + r"\s+)*"
_G_CONTRAST = r"(?:rather\s+than|instead\s+of|not|never|nor)" + _G_TB
_G_PROHIBITION = r"(?:never|do\s+not|don't|must\s+not|mustn't|may\s+not)" + _G_TB
_G_UNIT = r"(?:" + _G_HOST_TARGET + r"|" + _G_ROUTE + r"|" + _G_LOCUS + r")"
_G_H_PROHIBITION = (_G_PROHIBITION + r"(?:\s+" + _G_VERB + r")?(?:\s+" + _G_LAUNCH_OBJECT + r")?(?:\s+" + _G_UNIT
                    + r"(?:\s+(?:or|nor|and)" + _G_TB + r"\s+" + _G_UNIT + r")*)?")
_G_A = r"(?:" + _G_LOCUS + r"\s+)*" + _G_ROUTE + r"(?:\s+" + _G_ROUTE + r")*(?:\s+" + _G_LOCUS + r")*"
_G_H_ROUTED = _G_FILLERS + r"(?:" + _G_VERB + r"\s+)?(?:" + _G_LAUNCH_OBJECT + r"\s+)?" + _G_A + r"(?:\s+" + _G_CONTRAST + r"\s+" + _G_HOST_TARGET + r")?"
_G_H_BARE_VENUE = r"(?:(?:the|a|an)" + _G_TB + r"\s+)?" + _G_VENUE
_G_H_FRAG = (r"(?:" + _G_H_PROHIBITION + r"|" + _G_NEGATION + r"|" + _G_H_ROUTED + r"|" + _G_LOCUS + r"|" + _G_H_BARE_VENUE + r"|" + _G_OBJECT + r")")
_G_H_BARE_LAUNCH = _G_FILLERS + _G_VERB + r"(?:\s+" + _G_LAUNCH_OBJECT + r")?"
_G_H_SEPARATOR = r"(?:[.!?;:,]+(?=\s|$)|(?<=\s)-{1,2}(?=\s)|(?:and|or|then|but|so)" + _G_TB + r")"
_G_H_OPEN = (r"\s*(?:(?:#{1,6}|[*+>]+|\d{1,3}[.)])\s+)*(?:" + _G_H_SEPARATOR + r"\s*)*(?:" + _G_H_FRAG + r"(?:\s*" + _G_H_SEPARATOR + r")+\s*)*")
_G_HEAD_GRAMMAR = re.compile(_G_H_OPEN + r"(?:(?:" + _G_H_FRAG + r"|" + _G_H_BARE_LAUNCH + r")\s*)?", re.I)


def _head_is_benign(head: str) -> bool:
    return _G_HEAD_GRAMMAR.fullmatch(head) is not None


def _e1_terminal_in_grammar(text: str) -> bool:
    """True when `text` is one line whose first `locally <prep> <venue>` phrase is followed only by the closed allow-grammar, and
    everything before it is in the closed head grammar (r12: no earlier launch, host or unlisted word the grammar does not name)."""
    if "\n" in text:
        return False
    m = _G_E1.match(text)
    if not m or not _head_is_benign(m.group("head")):
        return False
    return _G_TAIL.fullmatch(text[m.end():]) is not None


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
    for group, refuse, text in _r8_rows(lists):
        add(group, refuse, text)
    for group, refuse, text in _r10_rows(lists):
        add(group, refuse, text)
    for group, refuse, text in _r11_rows(lists):
        add(group, refuse, text)
    for group, refuse, text in _r12_rows(lists):
        add(group, refuse, text)
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
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R8 + PromptLintTests.CLAUSE_MUST_PASS_R8
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R10 + PromptLintTests.CLAUSE_MUST_REFUSE_R10_OVERREFUSAL
                                  + PromptLintTests.CLAUSE_MUST_PASS_R10
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R11 + PromptLintTests.CLAUSE_MUST_PASS_R11
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R12 + PromptLintTests.CLAUSE_MUST_PASS_R12
                                  + PromptLintTests.CLAUSE_MUST_REFUSE_R13 + PromptLintTests.CLAUSE_MUST_PASS_R13
                                  + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS + RULE4_BOTH_ROUTED_PASS
                                  + RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_R2_PASS + RULE4_R2_B_APP_REFUSE + RULE4_R4_SEEDS + RULE4_R6_SEEDS + RULE4_R7_SEEDS
                                  + RULE4_R10_SEEDS
                                  + PromptLintTests.PROBE_ROWS
                                  + tuple(text for _, _, text in PromptLintTests.DEFEATER_AXIS) + PromptLintTests.DEFEATER_AXIS_CONTROLS,
                                  members=tuple(_list_member_rows(_lint_word_lists(LINT_PS1, work))))
    texts = list(corpus)
    # Both lints run at the same time, each as up to BATCH_SHARDS pwsh processes (r12: ~32k rows, ~17 ms a row, does not fit one 600 s
    # process), and only for the (lint bytes, text) pairs no earlier test already computed (r3): the candidate lint's catalogue and
    # list-member rows are known by now, and a baseline that IS the candidate (the identical-baseline row) costs nothing at all.
    _prefetch_counts(work, [(baseline, texts, "base"), (LINT_PS1, texts, "cand")])
    before = _verdicts(baseline, texts)
    after = _verdicts(LINT_PS1, texts)
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


# NA2-RULE4-PER-LAUNCHER-SCOPE-1 (CoS post-merge Blocker on PR #448, ask 6216180502; the seventh outside-probe bypass). Rule 4 scored only
# the FIRST launcher occurrence of a sentence, so a venue-routed first launcher exempted an unrouted second one after a separator.
# One row per separator x second-launcher shape x backend x launcher form x route of the first launcher. A command line is assembled
# from SMOKE_SCRIPT / APP / SMOKE_MODE, so no literal launcher line sits in this file (the hub-VM hooks read the file text).
RULE4_BACKENDS = ("cpu", "cuda")
# (name, text between the two launchers, form of the second launcher: "run" = with a launch verb, "bare" = the command line alone)
RULE4_SEPARATORS = (
    ("semicolon", "; ", "bare"),
    ("semicolon-then", "; then ", "run"),
    ("comma-then", ", then ", "run"),
    ("sentence-then", ". Then ", "run"),
    ("then", " then ", "run"),
    ("and", " and ", "run"),
    ("semicolon-run", "; ", "run"),
)
RULE4_ROUTES = ("on Bachelor", "through the venue chain")
# A separator that ends the SENTENCE was already read per sentence on master (the second sentence is judged alone): those rows are
# regression guards, not RED rows.
RULE4_ALREADY_REFUSED_SEPARATORS = ("sentence-then",)


def _rule4_launcher(style: str, backend: str) -> str:
    script = f"tools/profiling/{SMOKE_SCRIPT}"
    return {
        "path": f"{script} -Backend {backend}",
        "pwsh": f"pwsh -File {script} -Backend {backend}",
        "amp": f"& ./{script} -Backend {backend}",
        "app": f"{APP} take01.dat {SMOKE_MODE}",
        # r2 styles: the script alone by name, and pwsh with options before -File. (A QUOTED `& '<path>' -Backend ...` call is not
        # matched by any rule-4 pattern at all, at 75cb00e1 and on master alike: reported open, not a row here.)
        "bare": f"{SMOKE_SCRIPT} -Backend {backend}",
        "pwshopt": f"pwsh -NoProfile -NonInteractive -File {script} -Backend {backend}",
    }[style]


def _rule4_styles(backend: str) -> tuple[str, ...]:
    return ("path", "pwsh", "amp", "app") if backend == RULE4_BACKENDS[0] else ("path", "pwsh", "amp")


def _rule4_second(form: str, launcher: str, route: str = "") -> str:
    core = f"run {launcher}" if form == "run" else launcher
    return f"{core} {route}".rstrip()


def _rule4_rows() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """-> (second launcher unrouted after a routed first: every row must REFUSE, rows that were already refused: guards,
    both launchers routed: every row must PASS)."""
    refuse: list[str] = []
    guards: list[str] = []
    allow: list[str] = []
    for style, backend, route in itertools.product(("path", "pwsh", "amp", "app"), RULE4_BACKENDS, RULE4_ROUTES):
        if style not in _rule4_styles(backend):
            continue
        first = f"The venue job calls {_rule4_launcher(style, RULE4_BACKENDS[0])} {route}"
        second = _rule4_launcher(style, backend)
        for name, sep, form in RULE4_SEPARATORS:
            (guards if name in RULE4_ALREADY_REFUSED_SEPARATORS else refuse).append(f"{first}{sep}{_rule4_second(form, second)}.")
            for second_route in RULE4_ROUTES:
                allow.append(f"{first}{sep}{_rule4_second(form, second, second_route)}.")
        # two launchers in ONE clause (a comma with no verb is not a clause boundary). Master already refuses the first form (the venue
        # phrase followed by a comma and a launcher is no locus); the second form is a shared route after a launcher list, which master
        # passed for BOTH launchers and which now refuses (accepted over-refusal: give each launcher its own route).
        guards.append(f"{first}, {second}.")
        refuse.append(f"The venue job calls {_rule4_launcher(style, RULE4_BACKENDS[0])}, {second} {route}.")
        # the UNROUTED launcher comes first: read first on master too
        guards.append(f"Run {second}; then run {_rule4_launcher(style, RULE4_BACKENDS[0])} {route}.")
    # the CoS repro lines, verbatim (ask 6216180502): the venue job's first launcher is routed, the second is not
    cos_script = f"tools/profiling/{SMOKE_SCRIPT}"
    cos_first = f"The venue job calls {cos_script} -Backend cpu on Bachelor"
    cos_lines = (f"{cos_first}; then run {cos_script} -Backend cpu.", f"{cos_first}; {cos_script} -Backend cuda.")
    guards.extend((f"Run {cos_script} -Backend cpu.", f"{cos_first}. Then run {cos_script} -Backend cpu."))
    return (tuple(dict.fromkeys(list(cos_lines) + refuse)), tuple(dict.fromkeys(guards)), tuple(dict.fromkeys(allow)))


RULE4_SECOND_LAUNCHER_REFUSE, RULE4_SECOND_LAUNCHER_GUARDS, RULE4_BOTH_ROUTED_PASS = _rule4_rows()

# r2 (CoS review of 75cb00e1: a launcher's lazy gap crossed a `;` and swallowed the NEXT launcher, so the merged span took the first
# clause's venue route). The r1 sets above are unchanged; everything below is new in r2.
#   cross-clause   a venue-routed lead that is NOT itself a launcher ("pwsh -File build.ps1 on Bachelor", "<app> <file> on Bachelor")
#                  followed by an UNROUTED launcher after each separator. The same-style pairs (pwsh + pwsh, app + app) are the CoS repro
#                  shapes and PASSED at 75cb00e1 (RULE4_R2_CROSS_RED); the mixed-style pairs were refused there already.
#   "or"           the separator the lint header promises (`or` is a Disjunct cut): the r1 product with " or " as the separator.
#   single         one launcher per clause: routed passes, unrouted refuses. The verdict must equal the r1 verdict (parity).
#   venue on B     the route belongs to launcher B only: with A a launcher A is refused; with A not a launcher the sentence passes.
#   mixed styles   pwsh + app, & ./path + bare script name, pwsh with options before -File.
RULE4_R2_SEPARATORS = RULE4_SEPARATORS + (("or", " or ", "run"),)
RULE4_R2_PREFIXES = ("", "The venue job calls ")
RULE4_R2_MIXED_PAIRS = (("pwsh", "app"), ("app", "pwsh"), ("path", "amp"), ("amp", "path"), ("amp", "bare"), ("bare", "amp"),
                        ("pwsh", "bare"), ("app", "path"), ("pwshopt", "app"), ("bare", "pwshopt"))
RULE4_R2_SINGLE_STYLES = ("path", "pwsh", "amp", "app", "bare", "pwshopt")


def _rule4_lead(style: str, route: str) -> str:
    """A venue-routed lead that is not itself a playback launcher: a pwsh call of another script, or the app with a file and no mode."""
    return {"pwsh": f"pwsh -File build.ps1 {route}", "app": f"{APP} take01.dat {route}"}[style]


def _rule4_r2_rows() -> dict[str, tuple[str, ...]]:
    sets: dict[str, list[str]] = {k: [] for k in (
        "cross_refuse", "cross_red", "cross_guards", "cross_allow", "or_refuse", "or_allow", "single_refuse", "single_pass",
        "b_refuse", "b_pass", "b_app_refuse", "mixed_refuse", "mixed_allow")}
    cos = f"tools/profiling/{SMOKE_SCRIPT}"
    # the two CoS repro lines of review 6218718347 (a pwsh pair and an app pair), verbatim shapes
    cos_pwsh = f"pwsh -File build.ps1 on Bachelor; pwsh -File {cos} -Backend cpu."
    cos_app = f"{APP} take01.dat on Bachelor; then run {APP} take01.dat {SMOKE_MODE}."
    for row in (cos_pwsh, cos_app):
        sets["cross_refuse"].append(row)
        sets["cross_red"].append(row)
    for prefix, lead_style, route in itertools.product(RULE4_R2_PREFIXES, ("pwsh", "app"), RULE4_ROUTES):
        lead = f"{prefix}{_rule4_lead(lead_style, route)}"
        for second_style in ("pwsh", "path", "amp", "app", "bare", "pwshopt"):
            second = _rule4_launcher(second_style, RULE4_BACKENDS[0])
            for name, sep, form in RULE4_R2_SEPARATORS:
                bucket = "cross_guards" if name in RULE4_ALREADY_REFUSED_SEPARATORS else "cross_refuse"
                sets[bucket].append(f"{lead}{sep}{_rule4_second(form, second)}.")
                # same family as the lead (a pwsh lead + a pwsh -File second, an app lead + an app second) = the CoS repro shapes
                # (the options form is RED at 75cb00e1 only behind the plain route; behind the chain route that head refused it already)
                if bucket == "cross_refuse" and ((lead_style, second_style) in (("pwsh", "pwsh"), ("app", "app"))
                                                 or ((lead_style, second_style) == ("pwsh", "pwshopt") and route == RULE4_ROUTES[0])):
                    sets["cross_red"].append(f"{lead}{sep}{_rule4_second(form, second)}.")
                for second_route in RULE4_ROUTES:
                    sets["cross_allow"].append(f"{lead}{sep}{_rule4_second(form, second, second_route)}.")
    # " or " over the r1 product (same-style pairs, a launcher as the lead)
    for style, backend, route in itertools.product(("path", "pwsh", "amp", "app"), RULE4_BACKENDS, RULE4_ROUTES):
        if style not in _rule4_styles(backend):
            continue
        first = f"The venue job calls {_rule4_launcher(style, RULE4_BACKENDS[0])} {route}"
        second = _rule4_launcher(style, backend)
        sets["or_refuse"].append(f"{first} or run {second}.")
        for second_route in RULE4_ROUTES:
            sets["or_allow"].append(f"{first} or run {second} {second_route}.")
    # single-launcher clauses: parity with r1
    for style, backend in itertools.product(RULE4_R2_SINGLE_STYLES, RULE4_BACKENDS):
        if style == "app" and backend != RULE4_BACKENDS[0]:
            continue
        launcher = _rule4_launcher(style, backend)
        for lead in ("Run", "The venue job calls", "Then run"):
            sets["single_refuse"].append(f"{lead} {launcher}.")
            for route in RULE4_ROUTES:
                sets["single_pass"].append(f"{lead} {launcher} {route}.")
    # the route belongs to launcher B only
    for a_style, b_style in itertools.product(("path", "pwsh", "amp", "app", "bare"), repeat=2):
        a = _rule4_launcher(a_style, RULE4_BACKENDS[0])
        b = _rule4_launcher(b_style, RULE4_BACKENDS[1] if b_style != "app" else RULE4_BACKENDS[0])
        for sep in ("; ", "; then run ", " and run "):
            sets["b_refuse"].append(f"Run {a}{sep}{b} on Bachelor.")
    for lead_style, b_style in itertools.product(("pwsh", "app"), ("path", "pwsh", "amp", "app", "bare")):
        lead = {"pwsh": "pwsh -File build.ps1", "app": f"{APP} take01.dat"}[lead_style]
        b = _rule4_launcher(b_style, RULE4_BACKENDS[0])
        for sep in ("; ", "; then run ", " and run "):
            # r9 (hub ruling on CoS review 6230178238, B5): `<app> <clip>` IS an app launch of its own (a launch is decided from the app
            # token and its own argument gap), so an app lead with no route of its own is refused although launcher B is routed. Those
            # 15 rows left the routed-pass set for RULE4_R2_B_APP_REFUSE (an accepted over-refusal, named in the PR body).
            sets["b_app_refuse" if lead_style == "app" else "b_pass"].append(f"{lead}{sep}{b} on Bachelor.")
    # mixed-style pairs: the first launcher routed, the second not (refuse) / both routed (allow)
    for (first_style, second_style), (name, sep, form), route in itertools.product(RULE4_R2_MIXED_PAIRS, RULE4_R2_SEPARATORS, RULE4_ROUTES):
        if name in RULE4_ALREADY_REFUSED_SEPARATORS:
            continue
        first = f"The venue job calls {_rule4_launcher(first_style, RULE4_BACKENDS[0])} {route}"
        second = _rule4_launcher(second_style, RULE4_BACKENDS[0])
        sets["mixed_refuse"].append(f"{first}{sep}{_rule4_second(form, second)}.")
        sets["mixed_allow"].append(f"{first}{sep}{_rule4_second(form, second, route)}.")
    return {k: tuple(dict.fromkeys(v)) for k, v in sets.items()}


_RULE4_R2 = _rule4_r2_rows()
RULE4_R2_CROSS_REFUSE = _RULE4_R2["cross_refuse"]
RULE4_R2_CROSS_RED = _RULE4_R2["cross_red"]
RULE4_R2_CROSS_GUARDS = _RULE4_R2["cross_guards"]
RULE4_R2_CROSS_ALLOW = _RULE4_R2["cross_allow"]
RULE4_R2_OR_REFUSE = _RULE4_R2["or_refuse"]
RULE4_R2_OR_ALLOW = _RULE4_R2["or_allow"]
RULE4_R2_SINGLE_REFUSE = _RULE4_R2["single_refuse"]
RULE4_R2_SINGLE_PASS = _RULE4_R2["single_pass"]
RULE4_R2_B_REFUSE = _RULE4_R2["b_refuse"]
RULE4_R2_B_PASS = _RULE4_R2["b_pass"]
# r9: the 15 rows `<app> <clip><sep><routed script> on Bachelor.` (a routed launcher B behind an app lead with no route of its own). The
# r2 sets held them as ALLOW rows; the r9 rule (an app token with an argument is a launch of its own) REFUSES them, so they are a named
# refuse set, an accepted over-refusal (hub ruling on CoS review 6230178238): the first clause is an app launch with a clip and no route.
RULE4_R2_B_APP_REFUSE = _RULE4_R2["b_app_refuse"]
# the three B_PASS rows the r1 head refused (a same-family non-launcher lead: its gap swallowed launcher B), 3 separators x pwsh. (r2
# listed six, with the app form; since r9 the app form is a launch of its own and sits in RULE4_R2_B_APP_REFUSE.)
RULE4_R2_B_OPENED = tuple(f"{lead}{sep}{_rule4_launcher(style, RULE4_BACKENDS[0])} on Bachelor."
                          for lead, style in (("pwsh -File build.ps1", "pwsh"),)
                          for sep in ("; ", "; then run ", " and run "))
RULE4_R2_MIXED_REFUSE = _RULE4_R2["mixed_refuse"]
RULE4_R2_MIXED_ALLOW = _RULE4_R2["mixed_allow"]
RULE4_R2_REFUSE = RULE4_R2_CROSS_REFUSE + RULE4_R2_OR_REFUSE + RULE4_R2_B_REFUSE + RULE4_R2_MIXED_REFUSE
RULE4_R2_GUARDS = RULE4_R2_CROSS_GUARDS + RULE4_R2_SINGLE_REFUSE
RULE4_R2_PASS = RULE4_R2_CROSS_ALLOW + RULE4_R2_OR_ALLOW + RULE4_R2_SINGLE_PASS + RULE4_R2_B_PASS + RULE4_R2_MIXED_ALLOW

# r4 (sol r3 CHANGES_REQUESTED 6223587477, CoS / Iverson advisory 6223667455, both on 4b9c9387). The r3 head is the RED baseline of
# these rows (PromptLintTests.test_rule4_r4_*).
#   quoted     sol: a routed first launcher, then an UNROUTED quoted call `& '<script>' -Backend cuda` after `; `, `, then`, ` then`,
#              `. Then` and a line break. No rule-4 pattern matched the quoted spelling at all (the closing quote sat between the
#              script name and its parameter), so every row passed. The double-quoted spelling and a quoted app call are the same shape.
#   path arg   Iverson: the app's argument gap refused any token that merely CONTAINED a launcher name, so a path argument such as
#              `--log C:\MLVApp\out.txt` ended the gap before the mode flag and the launch was never seen. The verbatim repro strings
#              (comment 6223667455) are the first rows; the pwsh gap had the same stop (`-WorkingDirectory C:\MLVApp\w -File <script>`).
RULE4_R3_PR_HEAD = "4b9c938773a7d5995190d883c57ef46c0ec8e5c6"
RULE4_R4_SEPARATORS = (("semicolon", "; "), ("comma-then", ", then "), ("then", " then "), ("sentence-then", ". Then "), ("newline", "\n"))


def _rule4_r4_rows() -> dict[str, tuple[str, ...]]:
    sets: dict[str, list[str]] = {k: [] for k in ("red", "guards", "pass")}
    script = f"tools/profiling/{SMOKE_SCRIPT}"
    app_release = "MLVApp" + "Release.exe"
    first = f"The venue job calls {script} -Backend cpu on Bachelor"
    quoted = (f"& '{script}' -Backend cuda", f'& "{script}" -Backend cuda')
    for (_, sep), call in itertools.product(RULE4_R4_SEPARATORS, quoted):
        sets["red"].append(f"{first}{sep}{call}.")
        sets["pass"].append(f"{first}{sep}{call} on Bachelor.")
    for call in quoted + (f"& '{APP}' take01.dat {SMOKE_MODE}", f"& 'C:\\build\\{app_release}' take01.dat {SMOKE_MODE}"):
        sets["red"].append(f"Run {call}.")
        sets["pass"].append(f"The venue job calls {call} on Bachelor.")
    # Iverson's repro strings, verbatim: the two fail-open rows, then the plain-path control (refused at the r3 head too: a guard)
    iverson = (f"Run {app_release} --log C:\\MLVApp\\out.txt {SMOKE_MODE}", f"Run {app_release} --out C:\\pwsh\\x.txt {SMOKE_MODE}")
    sets["red"].extend(iverson)
    sets["guards"].append(f"Run {app_release} --clip D:\\clips\\a.mlv {SMOKE_MODE}")
    # the same stop on other path segments, and in the pwsh gap
    sets["red"].extend((f"Run {app_release} --log C:\\MLVAppRelease\\out.txt {SMOKE_MODE}",
                        f"Run {app_release} --out C:\\powershell\\x.txt {SMOKE_MODE}",
                        f"Run {app_release} --log D:\\{APP}.d\\log.txt {SMOKE_MODE}",
                        f"Run pwsh -NoProfile -WorkingDirectory C:\\MLVApp\\w -File {script}"))
    # a launcher token that is a real second launcher still stops the gap, and the second launcher is scored on its own route
    sets["guards"].append(f"Run pwsh -NoProfile -WorkingDirectory C:\\pwsh\\w -File {script}")
    sets["guards"].append(f"Run {app_release} take01.dat on Bachelor and run C:\\build\\{app_release} take01.dat {SMOKE_MODE}.")
    for row in iverson + (f"Run {app_release} --clip D:\\clips\\a.mlv {SMOKE_MODE}", f"Run pwsh -NoProfile -WorkingDirectory C:\\MLVApp\\w -File {script}"):
        sets["pass"].append(f"{row} on Bachelor.")
    # r4b (hub ruling, HUB-TICK 20261016T2244Z): a launch verb + the app + a clip argument is an app LAUNCH, so its own clause must carry
    # the venue; the route on launcher B never covers it. The first row is the differential-pin seed master and d2f3cd05 refuse and the
    # r3 / r4 heads passed (the r4 gap stops at the second app token, so the first app matched no pattern). A VERBLESS "<app> <clip>"
    # lead stays the r2 non-launcher lead (RULE4_R2_B_PASS pins it; no new row of that shape is added here, so nothing opens against
    # master or d2f3cd05 beyond r2's listed B_PASS opening), and a lead that carries its own route passes: the control below.
    sets["red"].extend((f"Run {app_release} take01.dat and run C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor.",
                        f"Run {APP} take01.dat and run {APP} take01.dat {SMOKE_MODE} on Bachelor.",
                        f"Run {app_release} take01.dat; run C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor.",
                        f"Open {APP} D:\\clips\\a.mlv, then run {APP} take01.dat {SMOKE_MODE} on Bachelor.",
                        f"Run {app_release} take01.dat and run {script} -Backend cpu on Bachelor."))
    sets["pass"].append(f"Run {app_release} take01.dat on Bachelor and run C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor.")
    return {k: tuple(dict.fromkeys(v)) for k, v in sets.items()}


_RULE4_R4 = _rule4_r4_rows()
RULE4_R4_RED = _RULE4_R4["red"]
RULE4_R4_GUARDS = _RULE4_R4["guards"]
RULE4_R4_PASS = _RULE4_R4["pass"]
RULE4_R4_REFUSE = RULE4_R4_RED + RULE4_R4_GUARDS
# the differential corpus is one text per line: the line-break rows are judged by the catalogue tests only
RULE4_R4_SEEDS = tuple(t for t in RULE4_R4_REFUSE + RULE4_R4_PASS if "\n" not in t)

# r6 (CoS review 6227161437 on 469e513d: Blocker B1, Improvement 1, Prevention; CoS Dot fold-in 6228249768: Blocker B2). The r4b head is the
# RED baseline of these rows (PromptLintTests.test_rule4_r6_*). The r4b clip-launch pattern took a bare first argument with an extension and
# nothing else, so an app launch whose clip follows an option (`--clip <clip>`, `-v <clip>`, `--input <path>`), has no extension, or is a
# quoted path with a space never counted as a launch of its own: a venue route on the NEXT launcher then covered it. Master refused every
# one of the B1 / B2 rows, so against master (and the pre-change pin) they are guards, not RED rows; Improvement 1's two rows pass on master
# too. Fixture clips follow this file's clip-name convention (take01.dat, a.dat): the lint's clip test is extension-generic.
#   axis     the clip-argument axis the Prevention note asks for: argument form x lead verb x separator x the launcher that carries the route
#            (an app launch with a bare clip, a tracked script call), in both orders (the clip launch first, and as the SECOND launcher).
RULE4_R4B_PR_HEAD = "469e513d6151655a0d11d6f3bd5c97f5190dcb40"
RULE4_R6_LEADS = ("Run", "Open")
RULE4_R6_SEPARATORS = ("; run ", ", then run ", " and run ")


def _rule4_r6_rows() -> dict[str, tuple[str, ...]]:
    app_release = "MLVApp" + "Release.exe"
    script = f"tools/profiling/{SMOKE_SCRIPT}"
    # argument forms the r4b pattern did not read as a clip launch (RED) and the ones it already read (guards)
    red_args = ("--clip take01.dat", "-v take01.dat", "--input D:\\clips\\a.dat", "D:\\clips\\take01", "--fps 24 take01.dat", "-v --clip take01.dat",
                "'D:\\my clips\\a.dat'", '"D:\\my clips\\a.dat"', "--clip 'D:\\my clips\\a.dat'", '--clip "D:\\my clips\\a.dat"')
    guard_args = ("take01.dat", "D:\\clips\\a.dat", "'D:\\clips\\a.dat'", '"D:\\clips\\a.dat"')
    routed_seconds = (f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor", f"pwsh -File {script} -Backend cpu on Bachelor")
    routed_first = f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor"
    sets: dict[str, list[str]] = {k: [] for k in ("red", "guards", "pass", "named")}
    for args, key in ((red_args, "red"), (guard_args, "guards")):
        for lead, sep, arg in itertools.product(RULE4_R6_LEADS, RULE4_R6_SEPARATORS, args):
            for second in routed_seconds:
                # the clip launch is the FIRST launcher and has no route of its own; the route on launcher B must not cover it
                sets[key].append(f"{lead} {app_release} {arg}{sep}{second}.")
                sets["pass"].append(f"{lead} {app_release} {arg} on Bachelor{sep}{second}.")
            # the clip launch is the SECOND launcher and has no route of its own (Improvement 1)
            sets[key].append(f"{lead} {routed_first}{sep}{app_release} {arg}.")
            sets["pass"].append(f"{lead} {routed_first}{sep}{app_release} {arg} on Bachelor.")
    # the CoS rows, verbatim in shape: B1 (six), Improvement 1 (two), B2 (two)
    second = f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor."
    sets["named"].extend((f"Run {app_release} --clip take01.dat and run {second}",
                          f"Run {app_release} --clip take01.dat; run {second}",
                          f"Run {app_release} --clip take01.dat, then run {second}",
                          f"Run {app_release} -v take01.dat and run {second}",
                          f"Run {app_release} --input D:\\clips\\a.dat and run {second}",
                          f"Run {app_release} D:\\clips\\take01 and run {second}",
                          f"Run C:\\build\\{app_release} take01.dat {SMOKE_MODE} on Bachelor and run {app_release} --clip take02.dat.",
                          f"Run {app_release} --clip D:\\clips\\a.dat and run pwsh -File {script} -Backend cpu on Bachelor.",
                          f"Run {app_release} 'D:\\my clips\\a.dat' and run {second}",
                          f'Run {app_release} "D:\\my clips\\a.dat" and run {second}'))
    # every CoS row is a RED row against the r4b head (the axis holds most of them; two are in no axis cell)
    sets["red"].extend(sets["named"])
    return {k: tuple(dict.fromkeys(v)) for k, v in sets.items()}


_RULE4_R6 = _rule4_r6_rows()
RULE4_R6_RED = _RULE4_R6["red"]
RULE4_R6_GUARDS = _RULE4_R6["guards"]
RULE4_R6_PASS = _RULE4_R6["pass"]
RULE4_R6_NAMED = _RULE4_R6["named"]
RULE4_R6_REFUSE = RULE4_R6_RED + RULE4_R6_GUARDS
RULE4_R6_SEEDS = tuple(RULE4_R6_REFUSE + RULE4_R6_PASS)

# r7 (CoS review 6228973410 on 0fb1eb8c: Blocker B3, N1, N2, Prevention). The r6 head is the RED baseline of these rows
# (PromptLintTests.test_rule4_r7_*). r4b and r6 each read a LIST of clip shapes and each list missed a form: B3 is `--clip=<clip>`, `/clip
# <clip>` and a bare token with no extension, and N2 is seven options before the clip. The r7 pattern reads no shape at all (any first
# argument after the app that is not the next launcher), and the sweep below is the check the CoS asked for: it is GENERATED from an
# argument grammar, not from the shapes anybody remembered, and it is judged against the pinned pre-change master, not against a list.
#   sweep    argument grammar (attached `=` values, `/` options, bare tokens, numbers, quoted values with and without a space, option
#            chains of 1-8 flags and of 1-8 flag + value pairs) x lead verb (with the app name it launches) x separator x the routed
#            launcher it sits next to (an app call, a script call) x the clip launch as the FIRST and as the SECOND launcher, once without
#            a route of its own (must refuse) and once with one (must pass).
#   fixtures the CoS rows name a real clip file; this round has no clip and NA-4 refuses that file extension in an edit, so every fixture keeps the
#            CoS row exactly (the `=` attachment, the `/` option, the bare token, the separators, the launcher twins) and uses this file's
#            `.dat` clip-name convention.
RULE4_R6_PR_HEAD = "0fb1eb8c5c2453d9f1d1d8f704a220da37637619"
RULE4_R7_LEAD_APPS = (("Run", "MLVApp" + "Release.exe"), ("Open", APP))
RULE4_R7_SEPARATORS = ("; run ", ", then run ", " and run ")
RULE4_R7_ROUTE = "on Bachelor"


def _rule4_r7_args() -> tuple[str, ...]:
    """The argument grammar: what can follow the app token of a launch, up to and including the clip."""
    attached = ("--clip=take01.dat", "--clip=D:\\clips\\a.dat", "-c=take01.dat", "--fps=24", "--clip='D:\\my clips\\a.dat'", '--clip="D:\\my clips\\a.dat"')
    slash = ("/clip take01.dat", "/v take01.dat", "/clip", "/clip D:\\clips\\a.dat")
    bare = ("take01", "take01.dat", "D:\\clips\\take01", "24", "3.5")
    quoted = ("'take01.dat'", '"take01.dat"', "'D:\\my clips\\a.dat'", '"D:\\my clips\\a.dat"', "--clip 'take01.dat'",
              "--clip 'D:\\my clips\\a.dat'", '--clip "D:\\my clips\\a.dat"')
    flags = tuple(" ".join(f"--o{i}" for i in range(1, n + 1)) + " take01.dat" for n in range(1, 9))
    pairs = tuple(" ".join(f"--o{i} v{i}" for i in range(1, n + 1)) + " take01.dat" for n in range(1, 9))
    return attached + slash + bare + quoted + flags + pairs


def _rule4_r7_rows() -> dict[str, tuple[str, ...]]:
    script = f"tools/profiling/{SMOKE_SCRIPT}"
    app_release = "MLVApp" + "Release.exe"
    twins = (f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE}", f"pwsh -File {script} -Backend cpu {RULE4_R7_ROUTE}")
    unrouted: list[str] = []
    routed: list[str] = []
    for (lead, app), sep, arg, twin in itertools.product(RULE4_R7_LEAD_APPS, RULE4_R7_SEPARATORS, _rule4_r7_args(), twins):
        # the clip launch is the FIRST launcher: the route on the next launcher must not cover it
        unrouted.append(f"{lead} {app} {arg}{sep}{twin}.")
        routed.append(f"{lead} {app} {arg} {RULE4_R7_ROUTE}{sep}{twin}.")
        # the clip launch is the SECOND launcher (the first one is routed)
        unrouted.append(f"{lead} {twin}{sep}{app} {arg}.")
        routed.append(f"{lead} {twin}{sep}{app} {arg} {RULE4_R7_ROUTE}.")
    # the CoS rows of review 6228973410, verbatim in shape (`.dat` fixtures): B3 (four, each with its `; run` / `, then run` twins) and N1
    second = f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE}."
    named: list[str] = []
    for sep in (" and run ", "; run ", ", then run "):
        named.extend((f"Run {app_release} --clip=take01.dat{sep}{second}",
                      f"Run {app_release} --clip=D:\\clips\\a.dat{sep}{second}",
                      f"Run {app_release} /clip take01.dat{sep}{second}",
                      f"Run {app_release} take01{sep}{second}"))
    named.append(f"Run C:\\build\\{app_release} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE} and run {app_release} --clip=take02.dat.")
    named_b3 = tuple(named[:12])
    return {"unrouted": tuple(dict.fromkeys(list(named) + unrouted)), "routed": tuple(dict.fromkeys(routed)), "named": tuple(named), "named_b3": named_b3}


_RULE4_R7 = _rule4_r7_rows()
RULE4_R7_REFUSE = _RULE4_R7["unrouted"]
RULE4_R7_PASS = _RULE4_R7["routed"]
RULE4_R7_NAMED = _RULE4_R7["named"]
RULE4_R7_NAMED_B3 = _RULE4_R7["named_b3"]
# The sweep's allow-list: a row the pre-change master refuses that passes on the head. It is EMPTY, and an entry needs a reason, so a
# refuse -> pass flip of the argument grammar cannot be added without saying why.
RULE4_R7_SWEEP_ALLOWED: dict[str, str] = {}
RULE4_R7_SEEDS = tuple(RULE4_R7_REFUSE + RULE4_R7_PASS)

# r8 (opus key on 03e7ae1b: Blocker + hardening 1-3). The r7 sweep generated the ARGUMENT after the app but fixed the LEAD at `Run <app>` /
# `Open <app>`, so a markdown code span, `Re-run <app>` and `Run the <app> ...` hid the first launch and a later routed launcher's venue
# covered it (a refuse -> pass flip against the pin). The r8 lint reads the lead by construction (any run of openers and plain words
# between the listed verb and the app), and this sweep adds the two axes that were missing, judged by the same pinned-master test:
#   lead wrapper  none, a backtick code span (with the second call bare and in a code span too), single quotes, double quotes, one word
#                 and two words between the verb and the app.
#   verb form     Run, run, Open, open, Launch, Start, Re-run, and `Then run`; the SECOND launcher takes the same verbs behind ` and `.
# The argument axis is the two shapes that matter for the lead (a bare clip, an attached `--clip=` value): r7's sweep already covers the
# argument grammar. The pwsh cost is bounded: ~1800 rows, one sharded prefetch, the time is in the PR body.
# Fixtures keep the `.dat` clip-name convention of this file (no clip this round; NA-4 refuses the real extension in an edit).
RULE4_R7_PR_HEAD = "03e7ae1b49d469d6be0139b528356bebea0cb8d7"
RULE4_R8_VERBS = ("Run", "run", "Open", "open", "Launch", "Start", "Re-run", "Then run")
RULE4_R8_SEPARATORS = ("; run ", ", then run ", " and run ")


def _rule4_r8_wraps(app: str, arg: str) -> tuple[tuple[str, bool], ...]:
    """The lead-wrapper axis: (the call as written after the verb, whether the other call is wrapped in a code span too)."""
    return ((f"{app} {arg}", False),
            (f"`{app} {arg}`", False),
            (f"`{app} {arg}`", True),
            (f"'{app}' {arg}", False),
            (f'"{app}" {arg}', False),
            (f"the {app} {arg}", False),
            (f"the new {app} {arg}", False))


def _rule4_r8_rows() -> dict[str, tuple[str, ...]]:
    script = f"tools/profiling/{SMOKE_SCRIPT}"
    app_release = "MLVApp" + "Release.exe"
    route = RULE4_R7_ROUTE
    twin_calls = (f"C:\\build\\{app_release} take01.dat {SMOKE_MODE}", f"pwsh -File {script} -Backend cpu")
    unrouted: list[str] = []
    routed: list[str] = []
    for verb, sep, arg, twin_call in itertools.product(RULE4_R8_VERBS, RULE4_R8_SEPARATORS, ("take01.dat", "--clip=take01.dat"), twin_calls):
        for call, span_twin in _rule4_r8_wraps(app_release, arg):
            twin = f"`{twin_call}` {route}" if span_twin else f"{twin_call} {route}"
            # the wrapped call is the FIRST launcher: the route on the next launcher must not cover it
            unrouted.append(f"{verb} {call}{sep}{twin}.")
            routed.append(f"{verb} {call} {route}{sep}{twin}.")
    # the wrapped call is the SECOND launcher (the first one is routed), behind the same verb forms
    for verb, arg, twin_call in itertools.product(RULE4_R8_VERBS, ("take01.dat", "--clip=take01.dat"), twin_calls):
        for call, span_twin in _rule4_r8_wraps(app_release, arg):
            first = f"`{twin_call}` {route}" if span_twin else f"{twin_call} {route}"
            sep = f" and {verb.lower()} "
            unrouted.append(f"Run {first}{sep}{call}.")
            routed.append(f"Run {first}{sep}{call} {route}.")
    # the opus rows: the code-span catalogue row with its `; run` / `, then run` twins, the second call with and without backticks, and the
    # two hardening rows (a hyphen-prefixed verb, a word between the verb and the app) with the same three separators
    second = f"C:\\build\\{app_release} take01.dat {SMOKE_MODE}"
    named: list[str] = []
    for sep in (" and run ", "; run ", ", then run "):
        named.append(f"Run `{app_release} take01.dat`{sep}`{second}` {route}.")
        named.append(f"Run `{app_release} take01.dat`{sep}{second} {route}.")
        named.append(f"Re-run {app_release} take01.dat{sep}{second} {route}.")
        named.append(f"Run the {app_release} build on take01.dat{sep}{second} {route}.")
    return {"unrouted": tuple(dict.fromkeys(list(named) + unrouted)), "routed": tuple(dict.fromkeys(routed)), "named": tuple(named)}


_RULE4_R8 = _rule4_r8_rows()
RULE4_R8_REFUSE = _RULE4_R8["unrouted"]
RULE4_R8_PASS = _RULE4_R8["routed"]
RULE4_R8_NAMED = _RULE4_R8["named"]
# Accepted over-refusal (opus note on 03e7ae1b, named in the PR body): any first argument after a launcher that follows a routed launch is
# a launch of its own, so a harmless second call refuses where the pin passes it. It fails closed; route it or split the sentence.
RULE4_R8_OVERREFUSAL = (f"Run C:\\build\\{'MLVApp' + 'Release.exe'} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE} and open {'MLVApp' + 'Release.exe'} --help.",)
# The r8 sweep's allow-list: a row the pre-change master refuses that passes on the head. EMPTY; an entry needs a reason.
RULE4_R8_SWEEP_ALLOWED: dict[str, str] = {}

# r9 (CoS review 6230178238 on d30d1365, B5 + the Prevention; hub ruling). The r8 lint read the lead before the app token as a list of shapes
# (openers, plain words, one path) and the CoS found 84 refuse -> pass rows in five families outside it. The r9 rule reads NO lead: an app
# token is a launch when its OWN argument gap (up to the next separator or launcher) holds an argument and no playback mode, whatever
# stands before it. The families below are the CoS rows (x six launch verbs x the three separators x two launcher twins); the fuzz is the
# Prevention: a seeded generator draws the lead instead of anybody listing it.
#   family 1  an unquoted path with a space          family 4  punctuation in the lead (`,` `:`)
#   family 2  a word with an apostrophe              family 5  no verb right before the app (`Run:`, `Then`, `First`, a code span, an
#   family 3  a hyphenated word                                environment-variable path, `the <app> build`)
# Fixtures keep this file's `.dat` clip-name convention (no clip this round; NA-4 refuses the real extension in an edit).
RULE4_R8_PR_HEAD = "d30d1365f05a5e9d014ed06fd25553068c450c4c"
RULE4_R9_VERBS = ("Run", "Execute", "Re-run", "Launch", "Kick off", "Play")
RULE4_R9_SEPARATORS = ("; run ", ", then run ", " and run ")
RULE4_R9_FUZZ_SEED = 20261017
RULE4_R9_FUZZ_ROWS = 3600
# (family, lead template). {v} verb, {a} app name, {c} the call `<app> take01.dat`, {k} the clip. A template that holds {c} is the whole
# first call; one that holds {a} and {k} writes the call itself (`the <app> build on <clip>`).
RULE4_R9_LEADS = (
    (1, "{v} C:\\Program Files\\MLVApp\\{c}"),
    (1, "{v} D:\\my tools\\bin\\{c}"),
    (2, "{v} Layi's {c}"),
    (2, "{v} the team's {c}"),
    (3, "{v} the x64-release {c}"),
    (3, "{v} pwsh-free {c}"),
    (3, "{v} the re-built {c}"),
    (4, "{v}, as before, {c}"),
    (4, "{v} again: {c}"),
    (4, "{v}, then {c}"),
    (5, "{v}: .\\{c}"),
    (5, "Then {c}"),
    (5, "First ~/bin/{c}"),
    (5, "{v}: build\\{c}"),
    (5, "First $env:BIN\\{c}"),
    (5, "First %BIN%\\{c}"),
    (5, "{v}: `{c}`"),
    (5, "First the {a} build on {k}"),
)


def _rule4_r9_twins() -> tuple[str, ...]:
    return (f"C:\\build\\{'MLVApp' + 'Release.exe'} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE}", f"pwsh -File tools/profiling/{SMOKE_SCRIPT} -Backend cpu {RULE4_R7_ROUTE}")


def _rule4_r9_rows() -> dict[str, tuple[str, ...]]:
    """The CoS families: `unrouted` (the first call has no route of its own: must refuse), `routed` (it has one: must pass), `named`
    (the CoS example rows, verbatim in shape), `family` (family number per unrouted row)."""
    unrouted: list[str] = []
    routed: list[str] = []
    family: dict[str, int] = {}
    for (fam, template), verb, sep, twin, app in itertools.product(RULE4_R9_LEADS, RULE4_R9_VERBS, RULE4_R9_SEPARATORS, _rule4_r9_twins(), (APP, "MLVApp" + "Release.exe")):
        lead = template.format(v=verb, a=app, c=f"{app} take01.dat", k="take01.dat")
        row = f"{lead}{sep}{twin}."
        unrouted.append(row)
        family.setdefault(row, fam)
        routed.append(f"{lead} {RULE4_R7_ROUTE}{sep}{twin}.")
    app_release = "MLVApp" + "Release.exe"
    second = f"C:\\build\\{app_release} take01.dat {SMOKE_MODE} {RULE4_R7_ROUTE}"
    named = []
    for sep in (" and run ", "; run ", ", then run "):
        named.extend((f"Run C:\\Program Files\\MLVApp\\{app_release} take01.dat{sep}{second}.",
                      f"Run Layi's {app_release} take01.dat{sep}{second}.",
                      f"Run the x64-release {app_release} take01.dat{sep}{second}.",
                      f"Run pwsh-free {app_release} take01.dat{sep}{second}.",
                      f"Run, as before, {app_release} take01.dat{sep}{second}.",
                      f"Run again: {app_release} take01.dat{sep}{second}.",
                      f"Run: .\\{app_release} take01.dat{sep}{second}.",
                      f"Then {APP} take01.dat{sep}{second}.",
                      f"First ~/bin/{app_release} take01.dat{sep}{second}."))
    return {"unrouted": tuple(dict.fromkeys(list(named) + unrouted)), "routed": tuple(dict.fromkeys(routed)), "named": tuple(named), "family": family}


_RULE4_R9 = _rule4_r9_rows()
RULE4_R9_REFUSE = _RULE4_R9["unrouted"]
RULE4_R9_PASS = _RULE4_R9["routed"]
RULE4_R9_NAMED = _RULE4_R9["named"]
RULE4_R9_FAMILY = _RULE4_R9["family"]
# The r9 sweep's allow-list: a row the pre-change master refuses that passes on the head. EMPTY; an entry needs a reason.
RULE4_R9_SWEEP_ALLOWED: dict[str, str] = {}


def _rule4_r9_draw_lead(rng: random.Random, arg: str = "take01.dat") -> str:
    """One random LEAD plus the app call `<app> <arg>` that ends it (r10: lifted out of the r9 fuzz so the tail-axis fuzz draws the same
    lead axis; with the default `arg` and the same `rng` it draws exactly what r9 drew, call for call)."""
    words = ("build", "release", "x64", "debug", "nightly", "fresh", "same", "new", "old", "again", "as", "at", "the", "a", "one", "more", "time")
    apostrophe = ("Layi's", "team's", "build's", "that's", "Layi's")
    hyphen = ("x64-release", "pwsh-free", "re-built", "one-off", "pre-release", "clip-free")
    punct = (",", ":", "*", "_", "[", "<", "{", "(", ")")
    attached = ("again,", "before,", "now:", "build:", "release,", "step:")
    spaced_paths = ("C:\\Program Files\\MLVApp\\", "D:\\my tools\\bin\\", "C:\\Some Dir\\x64 build\\", "E:\\the build\\out\\")
    env_paths = ("$env:BIN\\", "%BIN%\\", "~/bin/", "build\\", ".\\", "./", "/usr/local/bin/", "$HOME/bin/", "..\\out\\")
    verbs = RULE4_R9_VERBS + tuple(v.lower() for v in RULE4_R9_VERBS) + ("Open", "Start", "Then run", "run")
    openers = ("", "", "Then", "First", "Next", "Finally", "Run:", "Step 1:", "Now:", "Also,")
    apps = (APP, "MLVApp" + "Release.exe")
    tokens: list[str] = []
    for _ in range(rng.randrange(0, 5)):
        kind = rng.randrange(0, 8)
        if kind == 0:
            tokens.append(rng.choice(words))
        elif kind == 1:
            tokens.append(rng.choice(apostrophe))
        elif kind == 2:
            tokens.append(rng.choice(hyphen))
        elif kind == 3:
            tokens.append(rng.choice(punct))
        elif kind == 4:
            tokens.append(rng.choice(attached))
        elif kind == 5:
            tokens.append("`" + rng.choice(words) + "`")
        elif kind == 6:
            q = rng.choice(("'", '"'))
            tokens.append(q + rng.choice(words) + q)
        else:
            tokens.append(rng.choice(words + hyphen))
    head = rng.choice(openers)
    verb = rng.choice(verbs) if rng.randrange(0, 5) else ""
    prefix = rng.choice(("", "", spaced_paths[rng.randrange(0, len(spaced_paths))], env_paths[rng.randrange(0, len(env_paths))]))
    app = rng.choice(apps)
    call = f"{prefix}{app} {arg}"
    shape = rng.randrange(0, 5)
    if shape == 0:
        call = f"`{call}`"
    elif shape == 1:
        call = f"{prefix}'{app}' {arg}"
    elif shape == 2:
        call = f'{prefix}"{app}" {arg}'
    return " ".join(part for part in (head, verb, *tokens, call) if part)


def _rule4_r9_fuzz_rows(seed: int = RULE4_R9_FUZZ_SEED, count: int = RULE4_R9_FUZZ_ROWS) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """-> (unrouted rows, their routed twins). The Prevention of CoS review 6230178238: the LEAD (what stands between a verb, or no verb,
    or a verbless clause opener, and the app token) is drawn at random, not listed (`_rule4_r9_draw_lead`). Pools: plain words, words
    with an apostrophe or a hyphen, punctuation (`,` `:` `*` `_` `[` `<` `{` `(`), unquoted spaced paths, environment-variable and
    relative paths, code spans and quotes. The same seed gives the same rows on every machine (random.Random(int) is a Mersenne Twister)."""
    rng = random.Random(seed)
    twins = _rule4_r9_twins()
    unrouted: list[str] = []
    routed: list[str] = []
    seen: set[str] = set()
    guard = 0
    while len(unrouted) < count and guard < count * 50:
        guard += 1
        lead = _rule4_r9_draw_lead(rng)
        sep = rng.choice(RULE4_R9_SEPARATORS)
        twin = rng.choice(twins)
        row = f"{lead}{sep}{twin}."
        if row in seen:
            continue
        seen.add(row)
        unrouted.append(row)
        routed.append(f"{lead} {RULE4_R7_ROUTE}{sep}{twin}.")
    return tuple(unrouted), tuple(routed)


RULE4_R9_FUZZ_REFUSE, RULE4_R9_FUZZ_PASS = _rule4_r9_fuzz_rows()

# r10 (CoS review 6230713095 on 96cdd148, B6 + the soft note + Recurrence 5; hub ruling). A `;` between the app's argument and its
# playback mode cut the app gap (r2), the sentence then held no direct launch, the app-plus-argument call was never scored and the whole
# sentence passed (`Run <app> <clip>; add <mode>.`). r10 scores the clip launch whenever the sentence holds a mode flag anywhere. The rows
# below are the CoS rows plus the TAIL axis: what stands between the app's argument and the mode flag. Every earlier sweep fuzzed the lead
# (what comes BEFORE the app) and kept the mode flag in the same clause; the tail was never an axis (Recurrence 5: a narrowing's parity
# sweep fuzzes both sides of the token). Fixtures keep this file's `.dat` clip-name convention.
RULE4_R9_PR_HEAD = "96cdd148e1a690e0b4dc9382398e76808d0d1bf6"
RULE4_R10_FUZZ_SEED = 20261018
RULE4_R10_FUZZ_ROWS = 2400
PROFILE_MODE = "--profile-" + "playback"
# the tail templates ({m} the mode flag): the separator and the clause that stands between the app's argument and the mode flag. Tails
# with `here` are refused whatever the route says and so are never twinned.
RULE4_R10_TAILS = (
    ("semicolon", "; add {m}"),
    ("semicolon-then", "; then add {m}"),
    ("semicolon-no-space", ";add {m}"),
    ("semicolon-no-space-then", ";then add {m}"),
    ("spaced-semicolon", " ; add {m}"),
    ("spaced-semicolon-bare-mode", " ; {m}"),
    ("semicolon-bare-mode", "; {m}"),
    ("later-clause-with-a-verb", "; copy the log, then pass {m}"),
    ("later-clause-then-a-routed-run", "; add {m}; run the venue chain on Bachelor"),
    ("negated-later-clause", "; do not pass {m} anywhere, run it on Bachelor"),
    ("negated-later-clause-use", "; never use {m} locally, run it on Bachelor"),
)
RULE4_R10_ARGS = ("take01.dat", "take01.dat --verbose", "--clip=take01.dat", "-v take01.dat", "take01")


def _rule4_r10_glued(tail: str) -> bool:
    """A tail whose `;` has no space after it and is followed by a word that is no locus-preserving one. A route written right before it
    (`on Bachelor;add ...`) is no venue locus to the lint (the text after a punctuation mark must be the end or a locus-preserving word;
    `then` is one, so `on Bachelor;then add ...` is a route), on the pin and on every head alike: such a twin refuses everywhere, so it
    is no ALLOW row (RULE4_R10_GLUED_ROUTE pins that both agree)."""
    return tail.startswith(";") and tail[1:2].isalpha() and not tail.startswith(";then ")


def _rule4_r10_rows() -> dict[str, tuple[str, ...]]:
    """`named` (the CoS rows, fixtures with `.dat`), `grid` (verb x call x tail x mode), `routed` (the grid rows with the route on the app
    call itself: must pass), `glued` (the same with a `;` glued to the route: refused by the pin and the head alike), `noarg` (the soft
    note: the app token with no argument, `;` glued or spaced after it, then a mode: a tightening, the pin passes most of them),
    `noarg_routed` (the same with a route on the app call)."""
    app_release = "MLVApp" + "Release.exe"
    named = (f"Run {app_release} take01.dat; add {SMOKE_MODE}.",
             f"Run {app_release} take01.dat; use {SMOKE_MODE} here.",
             f"Start {APP} take01.dat; pass {PROFILE_MODE}.",
             f"Run {app_release} take01.dat;add {SMOKE_MODE}.",
             f"Run {app_release} take01.dat ; {PROFILE_MODE}",
             f"Run C:\\build\\{app_release} take01.dat --verbose; then add {SMOKE_MODE}.",
             f"Run {app_release} take01.dat; add {SMOKE_MODE}; run the venue chain on Bachelor.",
             f"Run {app_release} take01.dat; do not pass {SMOKE_MODE} here, run it on Bachelor.")
    grid: list[str] = []
    routed: list[str] = []
    glued: list[str] = []
    for verb, app, arg, (_, tail), mode in itertools.product(("Run", "Start", "Launch", "Re-run"), (APP, app_release), RULE4_R10_ARGS,
                                                             RULE4_R10_TAILS, (SMOKE_MODE, PROFILE_MODE)):
        call = f"{verb} {app} {arg}"
        grid.append(f"{call}{tail.format(m=mode)}.")
        (glued if _rule4_r10_glued(tail) else routed).append(f"{call} {RULE4_R7_ROUTE}{tail.format(m=mode)}.")
    noarg: list[str] = []
    noarg_routed: list[str] = []
    # every tail starts with a `;` (glued to the app token: `<app>; add`, `<app>;add`) or a spaced one (`<app> ; add`)
    for verb, app, (_, tail), mode in itertools.product(("Run", "Start"), (APP, app_release), RULE4_R10_TAILS, (SMOKE_MODE, PROFILE_MODE)):
        noarg.append(f"{verb} {app}{tail.format(m=mode)}.")
        if not _rule4_r10_glued(tail):
            noarg_routed.append(f"{verb} {app} {RULE4_R7_ROUTE}{tail.format(m=mode)}.")
    # a bare app mention (a build step, no argument) before a ROUTED direct launch is no launch: `$appBare` is scored only in a sentence
    # with a mode flag and no direct launch, so these keep passing (the pin passes them too). The spaced ` ; run ` form is left out: the
    # pin refuses it (its gap crossed the `;` and merged the mention with the routed launch) and r2 through r9 pass it, the same r2
    # narrowing as the three RULE4_R2_B_OPENED rows (named in the PR body).
    mention: list[str] = []
    for verb, app, sep, mode in itertools.product(("Build", "Compile", "Copy"), (APP, app_release), ("; then run ", ";run ", "; run "),
                                                  (SMOKE_MODE, PROFILE_MODE)):
        mention.append(f"{verb} {app}{sep}C:\\build\\{app_release} take01.dat {mode} {RULE4_R7_ROUTE}.")
        mention.append(f"{verb} {app}{sep}C:\\build\\{app_release} {mode} {RULE4_R7_ROUTE}.")
    # prose that lists the app and the flags with a bare ` / ` between them is a mention, not a launch (the catalogue row of
    # test_green_prompts_that_must_pass, which the first r10 cut refused)
    for app, mode in itertools.product((APP, app_release), (SMOKE_MODE, PROFILE_MODE)):
        mention.append(f"Grep for {app} / --seconds / --autoplay / {mode} in every launcher and apply the same oracle.")
        mention.append(f"Grep for {app} / {mode} in every launcher.")
    return {"named": named, "grid": tuple(dict.fromkeys(grid)), "routed": tuple(dict.fromkeys(routed)), "glued": tuple(dict.fromkeys(glued)),
            "noarg": tuple(dict.fromkeys(noarg)), "noarg_routed": tuple(dict.fromkeys(noarg_routed)), "mention": tuple(dict.fromkeys(mention))}


_RULE4_R10 = _rule4_r10_rows()
RULE4_R10_NAMED = _RULE4_R10["named"]
RULE4_R10_GRID = _RULE4_R10["grid"]
RULE4_R10_GRID_ROUTED = _RULE4_R10["routed"]
RULE4_R10_GLUED_ROUTE = _RULE4_R10["glued"]
RULE4_R10_NOARG = _RULE4_R10["noarg"]
RULE4_R10_NOARG_ROUTED = _RULE4_R10["noarg_routed"]
RULE4_R10_MENTION_PASS = _RULE4_R10["mention"]
RULE4_R10_REFUSE = tuple(dict.fromkeys(RULE4_R10_NAMED + RULE4_R10_GRID + RULE4_R10_NOARG))
# the rows the differential corpus carries as seeds (the CoS rows and the no-argument form); the grid and the fuzz are judged by the r10 tests
RULE4_R10_SEEDS = tuple(dict.fromkeys(RULE4_R10_NAMED + RULE4_R10_NOARG))
# The r10 sweep's allow-list: a row the pin refuses that passes on the head. EMPTY; an entry needs a reason.
RULE4_R10_SWEEP_ALLOWED: dict[str, str] = {}


def _rule4_r10_fuzz_rows(seed: int = RULE4_R10_FUZZ_SEED, count: int = RULE4_R10_FUZZ_ROWS) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """-> (unrouted rows, their routed twins). Recurrence 5 (CoS review 6230713095): the TAIL axis (RULE4_R10_TAILS: `;`, `; then`, `;` with
    no space, ` ; `, a later clause with a verb, a negated later clause, a later routed run) crossed with the LEAD axis of r9 (the same
    generator, `_rule4_r9_draw_lead`) and with the app's argument (RULE4_R10_ARGS) and the mode flag. The twin carries the route on the app
    call itself (none for a tail whose `;` is glued to the route: `_rule4_r10_glued`). The same seed gives the same rows on every machine."""
    rng = random.Random(seed)
    unrouted: list[str] = []
    routed: list[str] = []
    seen: set[str] = set()
    guard = 0
    while len(unrouted) < count and guard < count * 50:
        guard += 1
        arg = rng.choice(RULE4_R10_ARGS)
        lead = _rule4_r9_draw_lead(rng, arg)
        _, tail = rng.choice(RULE4_R10_TAILS)
        mode = rng.choice((SMOKE_MODE, PROFILE_MODE))
        row = f"{lead}{tail.format(m=mode)}."
        if row in seen:
            continue
        seen.add(row)
        unrouted.append(row)
        if not _rule4_r10_glued(tail):
            routed.append(f"{lead} {RULE4_R7_ROUTE}{tail.format(m=mode)}.")
    return tuple(unrouted), tuple(routed)


RULE4_R10_FUZZ_REFUSE, RULE4_R10_FUZZ_PASS = _rule4_r10_fuzz_rows()

# r9 quote-class pin (CoS review 6230178238, Prevention; the B4 root cause). In a PowerShell double-quoted string a backtick escapes the NEXT
# character, so `"['`"]"` silently evaluates to `['"]`: the class the author meant to hold a backtick does not. A comment can claim a
# quote set the pattern does not hold, and a test on rows only sees it when a row happens to use that character. This reads the EVALUATED
# strings: the lint is parsed (PowerShell AST), the assignments of the named pattern variables are evaluated in source order, and every
# regex character class that holds a quote character is compared with the set its comment claims. A class that lost a character, or a
# pattern that lost a class, fails. (names -> (the quote characters every quote-bearing class must hold, the least number of such classes))
RULE4_QUOTE_CLAIMS: dict[str, tuple[tuple[str, ...], int]] = {
    # r8: a backtick opens or closes an app token like a quote does (a markdown code span)
    "appStop": (("'", '"', "`"), 2),
    "clipLaunch": (("'", '"', "`"), 5),
    # r10: the app token with no argument and a `;` after it opens or closes like the other app tokens (a markdown code span)
    "appBare": (("'", '"', "`"), 1),
    "clipMention": (("'", '"', "`"), 5),
    # the pwsh token is quoted with ' or " only (r4 comment); the pwsh gap never claimed a backtick
    "pwshStop": (("'", '"'), 2),
    # a quotation of the incident wording opens with any of the three
    "incident": (("'", '"', "`"), 1),
}
# the r8 lead pieces (d30d1365); r9 deleted them with the lead lookbehind, the RED/GREEN proof below still reads them at that head
RULE4_QUOTE_CLAIMS_R8 = {**{k: v for k, v in RULE4_QUOTE_CLAIMS.items() if k not in ("appBare", "clipMention")}, "leadOpen": (("'", '"', "`"), 1), "leadWords": (("'", '"', "`"), 2), "leadPath": (("'", '"', "`"), 1)}
# A deliberate single backtick before a bracket or an apostrophe inside a double-quoted pattern string must be named here with its
# reason (none today; the `"` delimiter escape is the one legitimate single backtick and is covered by the evaluated check).
RULE4_BACKTICK_ESCAPES: dict[str, str] = {}
_QUOTE_PIECES = ("appStop", "appArg", "leadOpen", "leadWords", "leadPath")
_QUOTE_PROBE = r"""param([string]$Lint, [string]$Names, [string]$Out)
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Lint, [ref]$toks, [ref]$errs)
$want = @($Names -split ',')
$assigns = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and ($want -contains $n.Left.VariablePath.UserPath) }, $true) | Sort-Object { $_.Extent.StartOffset })
$verbs = 'run|open'; $b = '(?<![\w\-])'; $e = '(?![\w\-])'; $scripts = '(stub)\.ps1'
foreach ($a in $assigns) { Invoke-Expression $a.Extent.Text }
$values = @{}
foreach ($n in $want) { $v = Get-Variable -Name $n -ValueOnly -ErrorAction SilentlyContinue; if ($null -ne $v) { $values[$n] = @($v | ForEach-Object { [string]$_ }) } }
$dq = @($ast.FindAll({ param($n) ($n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) -and $n.StringConstantType -eq 'DoubleQuoted' }, $true) | ForEach-Object { $_.Extent.Text })
[System.IO.File]::WriteAllText($Out, (ConvertTo-Json -InputObject @{ values = $values; dq = $dq } -Compress -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
"""


def _regex_classes(pattern: str) -> list[str]:
    """The bodies of the regex character classes of `pattern` (an escaped bracket is no class)."""
    classes: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            i += 2
        elif pattern[i] == "[":
            j = i + 1
            if j < len(pattern) and pattern[j] == "^":
                j += 1
            while j < len(pattern) and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            classes.append(pattern[i + 1:j])
            i = j + 1
        else:
            i += 1
    return classes


def _single_backticks_before_bracket_or_quote(literal: str) -> list[str]:
    """In the source of a double-quoted PowerShell string: every SINGLE backtick (not part of a doubled one) directly before `[`, `]`,
    `(`, `)` or `'`, as the two-character sequence found. (A backtick before `"` is the delimiter escape and is not read here.)"""
    found: list[str] = []
    i = 0
    while i < len(literal) - 1:
        if literal[i] == "`":
            if literal[i + 1] in "[]()'":
                found.append(literal[i:i + 2])
            i += 2
        else:
            i += 1
    return found


def _lint_pattern_probe(lint: Path, names: tuple[str, ...], work: Path, tag: str) -> dict:
    """{'values': {name: [evaluated strings]}, 'dq': [source of every double-quoted literal]} of the lint at `lint`."""
    probe = work / f"{tag}-quote-probe.ps1"
    out = work / f"{tag}-quote-out.json"
    _write_probe(probe, _QUOTE_PROBE)
    # the pieces a composite pattern interpolates are evaluated too (in source order), or its value would hold an empty string for them
    wanted = tuple(dict.fromkeys(names + _QUOTE_PIECES))
    done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe), "-Lint", str(lint), "-Names", ",".join(wanted), "-Out", str(out)],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not out.exists():
        raise AssertionError(f"the quote-class probe failed: {(done.stderr or done.stdout).strip()[:300]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _quote_class_problems(lint: Path, claims: dict, work: Path, tag: str) -> list[str]:
    """Every way the evaluated patterns of `lint` differ from what their comments claim (empty = they hold every claimed quote character)."""
    data = _lint_pattern_probe(lint, tuple(claims), work, tag)["values"]
    problems: list[str] = []
    for name, (chars, least) in claims.items():
        if name not in data:
            problems.append(f"{name}: no assignment found")
            continue
        bearing = [c for value in data[name] for c in _regex_classes(value) if "'" in c or '"' in c]
        if len(bearing) < least:
            problems.append(f"{name}: {len(bearing)} quote class(es), at least {least} expected")
        for c in bearing:
            held = tuple(q for q in ("'", '"', "`") if q in c)
            if held != tuple(sorted(chars, key=("'", '"', "`").index)):
                problems.append(f"{name}: class [{c}] claims {chars!r} but holds {held!r}")
    return problems


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
    # r8 (CoS review of PR #448 r6 head 4957fff0, pr-448.md on bus c014c93f, Blocker 2, 6th round, fix option (i) sentence-terminal E1;
    # the "..." of the note is "Run playback locally on Bachelor"). Master refuses every row; r6 and r7 passed them through three routes:
    # (a) a weak negation anywhere in the later clause hid that clause's host reference, (b) a host noun outside the closed list,
    # (c) a follow-up sentence. The control row `..., not on Ultra-Magnus.` was already refused (bare `not`) and stays so.
    CLAUSE_MUST_REFUSE_R8 = (
        # (a) negation tail
        "Run playback locally on Bachelor, then run it here with no overlay.",
        "Run playback locally on Bachelor, then play the clip here, no audio.",
        "Run playback locally on Bachelor, then play the clip here if not busy.",
        "Run playback locally on Bachelor, then play the clip on my machine with no overlay.",
        "Run playback locally on Bachelor, then play the clip on the hub-VM with no overlay.",
        "Run playback locally on Bachelor; play the clip here with no overlay.",
        "Run playback locally on Bachelor, then run it here, no need to wait.",
        "Run playback locally on Bachelor, then open MLVApp here with no audio.",
        "Run playback locally on Bachelor, then play it here, no venue chain needed.",
        "Run playback locally on Bachelor, then run it here, not on Ultra-Magnus.",
        # (b) host nouns outside the closed list
        "Run playback locally on Bachelor and on this rig.",
        "Run playback locally on Bachelor and on this rig too.",
        "Run playback locally on Bachelor and on this instance.",
        "Run playback locally on Bachelor and in this container.",
        "Run playback locally on Bachelor and in this sandbox.",
        "Run playback locally on Bachelor and in the current session.",
        "Run playback locally on Bachelor and on the local install.",
        "Run playback locally on Bachelor and on this Windows install.",
        "Run playback locally on Bachelor and on this hypervisor.",
        # (c) a follow-up sentence
        "Run playback locally on Bachelor. Then play it here.",
        # r8's own finds while closing (a): a STRONG negation after the marker in the same clause ("don't wait") used to exempt the whole
        # clause, so the launch after it passed; a later launch verb the lint never listed ("replay") passed because no host was named.
        "Run playback locally on Bachelor, don't wait, then run it here.",
        "Run playback locally on Bachelor, don't worry, then play the clip on my machine.",
        "Run playback locally on Bachelor, must not stop, then open MLVApp on this rig.",
        "Run playback locally, don't use the venue chain.",
        "Run playback locally on Bachelor, and replay it.",
        "Run playback locally on Bachelor, then use this rig.",
        "Run playback locally on Bachelor, then check the log in this container.",
    )
    # r8, multi-line: a follow-up after a line break, a paragraph break or as the next list item is the SAME instruction block (only a
    # heading ends it). Kept out of the differential corpus, which is one sentence per line.
    CLAUSE_MUST_REFUSE_R8_MULTILINE = (
        "Run playback locally on Bachelor.\nThen play it here.\n",
        "Run playback locally on Bachelor.\n\nThen play it here.\n",
        "- Run playback locally on Bachelor.\n- Then play it here.\n",
        "1. Run playback locally on Bachelor.\n2. Open MLVApp here.\n",
        "Run playback locally on Bachelor.\nWrite the summary.\nThen run it on this rig.\n",
        "Run playback locally on Bachelor.\n\nRead the log.\n\nThen run it on this rig.\n",
    )
    CLAUSE_MUST_PASS_R8_MULTILINE = (
        # a heading ends the block: the later section is judged on its own
        "Run playback locally on Bachelor.\n\n## NEXT\nThen play it here.\n",
        "Run playback locally on Bachelor.\n\n## NEXT\nRead the log.\n\nWrite the summary to the run dir.\n",
        # r10: a later sentence of the block that is itself in the closed grammar keeps the exemption
        "Run playback locally on Bachelor.\nThen on Ultra-Magnus.\n",
        "Run playback locally on Bachelor.\n\nNot here.\n",
    )
    # r8 ALLOW list, as narrowed by r10: pure venue routing, a pure locus word and a verbless host negation still pass. (r8's handling
    # follow-ups moved to CLAUSE_MUST_REFUSE_R10_OVERREFUSAL.)
    CLAUSE_MUST_PASS_R8 = (
        "Run playback locally on Bachelor.",
        "Run playback locally on Bachelor and then on Ultra-Magnus.",
        "Run playback locally on Bachelor through the venue chain from here.",
        "Run playback locally on Bachelor, not here.",
        "Run playback locally on Bachelor, never on this host.",
    )

    # r6 ALLOW list: the venue route still works when no local-host reference appears anywhere in the sentence, across every joiner the
    # r6 rows use. (The exemption is narrowed to "nothing local is named", never removed.) r10 moved the rows whose follow-up is not in
    # the closed grammar to CLAUSE_MUST_REFUSE_R10_OVERREFUSAL; the two E2 rows are not E1 and keep passing.
    CLAUSE_MUST_PASS_R6 = (
        "Run playback locally on Bachelor.",
        "Run playback on Bachelor rather than locally; then read the log.",
        "Run playback on Bachelor through the venue chain; never locally.",
    )

    # r10 (hub ruling HUB-TICK 20261015T1650Z, STRICTLY TERMINAL E1). The r9 keys' repro sentences, verbatim: sol's blocker (a handling
    # tail hides a later launch; an adverb moves the verb out of the old LaunchVerb pattern; refusal depended on a listed host noun) and
    # opus's blocker (a venue-routing tail with a launch lead and a destination preposition) plus opus's three hardening rows. Every
    # row passed at r9 fd190b6d and is refused by master 61d501df.
    CLAUSE_MUST_REFUSE_R10 = (
        # sol r9 blocker
        "Run playback locally on Bachelor, then read the log to quietly replay it.",
        "Run playback locally on Bachelor. Then record yourself playing it.",
        "Run playback locally on Bachelor, then verify this tablet can smoothly play it.",
        "Run playback locally on Bachelor, then read the log to quietly launch it.",
        # opus r9 blocker
        "Run playback locally on Bachelor, then run the arms with the clip copied from Bachelor.",
        "Run playback locally on Bachelor, then play the clip from the venue chain.",
        "Run playback locally on Bachelor. Then run playback for the venue job.",
        # opus r9 hardening (HUB-VM-LINT-TAIL-HANDLING-EMBEDDED-LAUNCH-1)
        "Run playback locally on Bachelor, then verify MLVApp plays the clip.",
        "Run playback locally on Bachelor, then check the arms run.",
        "Run playback locally on Bachelor, then copy the clip, the GUI runs it.",
    )
    # Accepted over-refusal (ruling 20261015T1650Z): these passed at r9 and now REFUSE, because a follow-up that is not venue routing, a
    # pure locus word or a verbless host negation voids E1 even when it is harmless. Authors move such a follow-up into its own section
    # (after a heading). Every row is listed in the PR body. Master refuses every row as well; the last one r9 already refused (a later
    # bare `locally`), it is here because the ruling names it.
    CLAUSE_MUST_REFUSE_R10_OVERREFUSAL = (
        # from CLAUSE_MUST_PASS_R6
        "Run playback locally on Bachelor, then read the log.",
        "Run playback locally on Bachelor; read the report afterwards.",
        "Run playback locally on Bachelor; then summarize the result on Ultra-Magnus.",
        "Run playback locally through the venue chain, then post the result to Bachelor.",
        "Run playback locally using Ultra-Magnus and then compare the checksum on Bachelor.",
        "Run playback locally on Bachelor, not on this host; then run the report on Bachelor.",
        "Run playback locally on Bachelor - then verify the checksum through the venue chain.",
        # from CLAUSE_MUST_PASS_R8
        "Run playback locally on Bachelor, then again through the venue chain.",
        "Run playback locally on Bachelor. Do not run it here.",
        "Run playback locally on Bachelor. Read the log.",
        "Run playback locally on Bachelor. Then summarize the result on Ultra-Magnus.",
        "Run playback locally on Bachelor. Write the summary to the run dir.",
        # the ruling's own examples
        "Run playback locally on Bachelor, then post the result to Bachelor.",
        "Run playback locally on Bachelor; then run the report on Bachelor.",
        "Run playback locally on Bachelor; copy the log locally.",
    )
    CLAUSE_MUST_REFUSE_R10_MULTILINE = (
        # from CLAUSE_MUST_PASS_R8_MULTILINE (accepted over-refusal: a blank line is not a block boundary, only a heading is)
        "Run playback locally on Bachelor.\nRead the log.\n\nWrite the summary to the run dir.\n",
        "Run playback locally on Bachelor.\n\nThen quietly play it.\n",
    )
    # r11b (sol r10 key, executed): a hard-wrapped PURE venue-routing instruction. r10 required a piece that another piece of the block
    # follows to end in punctuation, so the wrap refused it (0 -> 1 against fd190b6d). Each piece only has to be in the closed grammar on
    # its own now; a fragment cut by a line break ("..., not\nhere") is not in the grammar by itself and still refuses (the R11 refusal rows).
    CLAUSE_MUST_PASS_R11_MULTILINE = (
        "Run playback locally on Bachelor\nthrough the venue chain.\n",
        "Run playback locally on Bachelor and\nvia the venue chain.\n",
        # was a r10 "accepted over-refusal"; each piece is a verbless host negation / venue routing on its own, so it is the same harmless shape
        "Run playback locally on Bachelor, not here\non Bachelor.\n",
        "Run playback locally on Bachelor,\nthen on Ultra-Magnus\nand never here.\n",
    )
    # r11b: line-break forms that must stay refused: a launch verb, a destination preposition or a cut host negation on the next line.
    CLAUSE_MUST_REFUSE_R11_MULTILINE = (
        "Run playback locally on Bachelor\nand then play it here.\n",
        "Run playback locally on Bachelor, not\nhere on this host.\n",
        "Run playback locally on Bachelor and\nreplay it to this rig.\n",
        "Run playback locally on Bachelor,\nthen read the log.\n",
        "Run the arms on the hub-VM.\nThen run playback locally on Bachelor\nthrough the venue chain.\n",
    )
    # r11b: the opus r10 key (an earlier launch is not read) and its relatives. Every row is refused at master; all but the first
    # control are clean at d291f184 (0 findings there). The sol r10 hard-wrap rows above are the opposite direction (refused at
    # d291f184, clean here).
    CLAUSE_MUST_REFUSE_R11 = (
        # opus r10 BLOCKER, verbatim
        "Run playback on the hub-VM and locally on Bachelor.",
        "Run playback on my rig, then locally on Bachelor.",
        "Run playback on my machine and locally on Bachelor.",
        "Run the arms on the hub-VM. Then run playback locally on Bachelor.",
        # the control that already refused
        "Run playback on this host, then locally on Bachelor.",
        # a host nobody listed in the marker clause's own prefix; a weak negation in the head; the head in an earlier ';' group
        "Run playback on my rig locally on Bachelor.",
        "Run playback on the hub-VM with no overlay and locally on Bachelor.",
        "Run playback on my rig; run playback locally on Bachelor.",
        "Run playback on my rig with no overlay, then run playback locally on Bachelor.",
        # an earlier clause with a verb the lint does not list, behind a preposition it does not need to know the object of
        "Replay the arms on my rig, then run playback locally on Bachelor.",
        "Perform playback under my rig, then run playback locally on Bachelor.",
        # accepted over-refusal (listed in the PR body): an earlier un-routed launch verb in the same sentence
        "Run the build, then run playback locally on Bachelor.",
        "Run playback on Bachelor with no overlay and locally on Bachelor.",
    )
    # r11b ALLOW: an earlier clause or item that is venue-routed with no host. These are in the differential corpus: master refuses every
    # `locally`, so each is a refuse -> pass flip that the Python grammar oracle (_head_is_benign) must explain.
    CLAUSE_MUST_PASS_R11 = (
        "Run playback on Bachelor, then locally on Bachelor.",
        "Run the arms on Bachelor and locally on Bachelor through the venue chain.",
        "Run the arms on Bachelor. Then run playback locally on Bachelor.",
        "Run playback through the venue chain; run playback locally on Bachelor.",
    )
    # r11b ALLOW: an earlier clause or item that a STRONG prohibition governs, or a contrast complement that rules the host out. The
    # oracle is deliberately narrower than the lint here, so these stay out of the differential corpus (direct tests only).
    CLAUSE_MUST_PASS_R11_PROHIBITION = (
        "Never run playback on this host, run playback locally on Bachelor.",
        "Do not run playback here. Run playback locally on Bachelor.",
        "Run playback on Bachelor, not on this host, and locally on Bachelor.",
        "Run playback on Bachelor rather than here, then run playback locally on Bachelor.",
    )
    # r12 (opus + sol r11b keys on c574ea89, repro rows verbatim): an EARLIER sentence or clause whose launch verb the lint does not
    # list (Re-run, Replay, Preview, Reproduce ...), or whose verb the old word boundary hid, ran playback somewhere local and hid behind
    # the venue-routed `locally`. Every row is refused by master, clean at c574ea89 (RED there) and refused here: the head is a closed
    # whitelist, so a word it does not name voids E1 whatever host it names, or none.
    CLAUSE_MUST_REFUSE_R12 = (
        # opus r11b BLOCKER
        "Re-run the arms on this host. Then run playback locally on Bachelor.",
        "Replay the arms on the hub-VM. Then run playback locally on Bachelor.",
        "Re-run the GUI smoke on my machine. Run playback locally on Bachelor.",
        # sol r11b BLOCKER (executed: master=1, c574ea89=0)
        "Replay the clip here. Then run playback locally on Bachelor.",
        "Replay the arms on my rig. Then run playback locally on Bachelor.",
        "Preview the clip on this tablet. Then run playback locally on Bachelor.",
        "Reproduce the playback here. Then run playback locally on Bachelor.",
        "Replay the clip with this local install, then run playback locally on Bachelor.",
        "Replay the clip with this local install; run playback locally on Bachelor.",
        "Replay the clip with this local install - then run playback locally on Bachelor.",
        "Replay the clip with this local install and then run playback locally on Bachelor.",
        # opus r11b hardening, now a refusal (HUB-VM-LINT-HEAD-UNPREPOSED-LOCUS-1 is closed by the whitelist)
        "Re-run the arms hub-side, then run playback locally on Bachelor.",
        "Re-run the arms on-box, then run playback locally on Bachelor.",
        # the same shapes with the other hyphenated / prefixed / unlisted forms and with no locus at all
        "Rerun the arms on this host. Then run playback locally on Bachelor.",
        "Repeat the arms here, then run playback locally on Bachelor.",
        "Replay the arms. Then run playback locally on Bachelor.",
        "Run-through the arms on this host; run playback locally on Bachelor.",
        # accepted over-refusal (PR body): an unlisted verb is not a token even when its route is a venue
        "Replay the arms on Bachelor, then run playback locally on Bachelor.",
    )
    # r12 ALLOW: shapes the closed head grammar names. Every row is refused by master (`locally` + playback + run), so each is a
    # refuse -> pass flip the Python restatement of the grammar (_head_is_benign) must explain.
    CLAUSE_MUST_PASS_R12 = (
        "Please run playback locally on Bachelor.",
        "Playback: run it locally on Bachelor.",
        "Run the M16-1243 arms locally on Bachelor.",
        "Run playback on Bachelor and then locally on Bachelor.",
        "Never run playback on this host; run playback locally on Bachelor.",
        "Do not run playback locally, run playback locally on Bachelor.",
    )
    # r12 multi-line rows. A heading line is an ITEM of its own block, so it is read by the head grammar like any other piece.
    CLAUSE_MUST_REFUSE_R12_MULTILINE = (
        "Replay the clip here.\nRun playback locally on Bachelor.\n",
        "Re-run the arms on this host.\n\nThen run playback locally on Bachelor.\n",
        "## Steps\nRun playback locally on Bachelor.\n",
        "## Venue\nRe-run the arms on my rig.\nRun playback locally on Bachelor.\n",
    )
    CLAUSE_MUST_PASS_R12_MULTILINE = (
        "## Through the venue chain\nRun playback locally on Bachelor.\n",
        "## Venue\nRun playback locally on Bachelor\nthrough the venue chain.\n",
    )
    # r13 (sol r12 BLOCKER HUB-VM-LINT-HEAD-EXECUTABLE-AS-OBJECT-1): an earlier BARE executable command (an executable, with or without a
    # file argument) is a launch with no route, never an object phrase, so it voids E1 and the text refuses. Sol's repro texts used a
    # real clip file; per the hub ruling of 2026-10-16 they are restated with a neutral made-up file argument (the rule is that ANY file
    # argument after an executable is a launch). Every row is refused by master, clean at d2f3cd05 (RED there) and refused here.
    CLAUSE_MUST_REFUSE_R13 = (
        "MLVApp.exe take01.dat; run playback locally on Bachelor.",
        "MLVApp.exe take01.dat. Then run playback locally on Bachelor.",
        "MLVApp.exe take01.dat, then run playback locally on Bachelor.",
        "MLVApp.exe and then run playback locally on Bachelor.",
    )
    CLAUSE_MUST_REFUSE_R13_MULTILINE = (
        "MLVApp.exe\nRun playback locally on Bachelor.\n",
        "MLVAppRelease.exe take01.dat\nRun playback locally on Bachelor.\n",
        "MLVApp.exe sample1.bin\n\nThen run playback locally on Bachelor.\n",
    )
    # r13 ALLOW controls: an executable that carries its OWN route, follows a prohibition, or is the marker clause's own launch.
    CLAUSE_MUST_PASS_R13 = (
        "Run MLVApp.exe take01.dat on Bachelor, then run playback locally on Bachelor.",
        "MLVApp.exe take01.dat on Bachelor; run playback locally on Bachelor.",
        "Never run MLVApp.exe, run playback locally on Bachelor.",
        "Run MLVApp.exe take01.dat locally on Bachelor.",
    )
    # r10 ALLOW list: tails in the closed grammar only -- (A) venue routing, (B) a pure locus word, (C) a verbless host negation.
    CLAUSE_MUST_PASS_R10 = (
        "Run playback locally on Bachelor, then on Ultra-Magnus.",
        "Run playback locally on Bachelor and via the venue chain.",
        "Run playback locally on Bachelor or through the chain runner.",
        "Run playback locally on Bachelor; afterwards.",
        "Run playback locally on Bachelor, not locally.",
        "Run playback locally on Bachelor, not on hub.",
        "Run playback locally on Bachelor, never on this same host.",
        "Run playback locally on Bachelor, not on localhost.",
        "Run playback locally on Bachelor. Not here.",
        "Run playback locally on Bachelor. Then on Ultra-Magnus, never here.",
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

    def test_red_every_later_clause_route_of_the_r6_review_is_refused(self):
        """r8: Blocker 2 rows (a) negation tail, (b) out-of-list host noun, (c) follow-up sentence, verbatim from the CoS note."""
        for text in self.CLAUSE_MUST_REFUSE_R8:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_red_r8_a_follow_up_across_a_line_break_or_list_item_is_refused(self):
        for text in self.CLAUSE_MUST_REFUSE_R8_MULTILINE:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)
        for text in self.CLAUSE_MUST_PASS_R8_MULTILINE:
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)

    def test_green_clause_scope_venue_routed_wordings_pass(self):
        for text in (self.CLAUSE_MUST_PASS + self.CLAUSE_MUST_PASS_R6 + self.CLAUSE_MUST_PASS_R7 + self.CLAUSE_MUST_PASS_R8 + self.CLAUSE_MUST_PASS_R10
                     + self.CLAUSE_MUST_PASS_R11 + self.CLAUSE_MUST_PASS_R11_PROHIBITION):
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)

    def test_red_r10_the_r9_key_rows_are_refused(self):
        """r10: every repro sentence of the r9 sol and opus keys (RED at fd190b6d, GREEN here)."""
        for text in self.CLAUSE_MUST_REFUSE_R10:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_red_r10_accepted_over_refusal_rows_are_refused(self):
        """r10, ruling 20261015T1650Z: a follow-up outside the closed grammar refuses, harmless or not (listed in the PR body)."""
        for text in self.CLAUSE_MUST_REFUSE_R10_OVERREFUSAL + self.CLAUSE_MUST_REFUSE_R10_MULTILINE:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_red_r11_an_earlier_launch_in_front_of_a_venue_routed_locally_is_refused(self):
        """r11b: every repro sentence of the opus r10 key (clean at d291f184, RED there; GREEN here) and its relatives."""
        for text in self.CLAUSE_MUST_REFUSE_R11:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_green_r11_a_venue_routed_or_prohibited_head_still_passes(self):
        for text in self.CLAUSE_MUST_PASS_R11 + self.CLAUSE_MUST_PASS_R11_PROHIBITION:
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)

    def test_r11_hard_wrapped_pure_venue_routing_passes_and_a_cut_fragment_still_refuses(self):
        """r11b, sol r10 key (executed): a hard wrap inside a pure venue-routing instruction is not a follow-up."""
        for text in self.CLAUSE_MUST_PASS_R11_MULTILINE:
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text), text)
        for text in self.CLAUSE_MUST_REFUSE_R11_MULTILINE:
            with self.subTest(text=text):
                self.assertTrue(self._findings(text), text)

    def test_red_r12_an_earlier_launch_with_an_unlisted_verb_is_refused(self):
        """r12: every repro row of the opus and sol r11b keys (clean at c574ea89, RED there; GREEN here), one pwsh for all of them."""
        rows = list(self.CLAUSE_MUST_REFUSE_R12 + self.CLAUSE_MUST_REFUSE_R12_MULTILINE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "r12-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r12 rows the lint passed that it must refuse")

    def test_green_r12_the_closed_head_grammars_own_shapes_pass(self):
        rows = list(self.CLAUSE_MUST_PASS_R12 + self.CLAUSE_MUST_PASS_R12_MULTILINE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "r12-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r12 rows the lint refused that it must pass")

    def test_red_r13_an_earlier_bare_executable_command_is_refused(self):
        """r13: sol's r12 repro rows (clean at d2f3cd05, RED there; GREEN here), restated with a neutral file argument."""
        rows = list(self.CLAUSE_MUST_REFUSE_R13 + self.CLAUSE_MUST_REFUSE_R13_MULTILINE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "r13-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r13 rows the lint passed that it must refuse")

    def test_green_r13_an_executable_carrying_its_own_route_passes(self):
        rows = list(self.CLAUSE_MUST_PASS_R13)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "r13-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r13 rows the lint refused that it must pass")

    def test_red_rule4_a_second_unrouted_launcher_is_refused_after_every_separator(self):
        """NA2-RULE4-PER-LAUNCHER-SCOPE-1: a venue locus exempts only the launcher it routes. The CoS repro lines (ask 6216180502) are
        the first two rows; the rest is separator x second-launcher shape x backend x launcher form x route. The guards (a separate
        sentence, an unrouted FIRST launcher) were refused before this change and must stay refused."""
        script = f"tools/profiling/{SMOKE_SCRIPT}"
        first = f"The venue job calls {script} -Backend cpu on Bachelor"
        for line in (f"{first}; then run {script} -Backend cpu.", f"{first}; {script} -Backend cuda."):
            self.assertIn(line, RULE4_SECOND_LAUNCHER_REFUSE)
        for line in (f"Run {script} -Backend cpu.", f"{first}. Then run {script} -Backend cpu."):
            self.assertIn(line, RULE4_SECOND_LAUNCHER_GUARDS)
        for sep in ("; ", ", then ", ". Then ", " then "):  # each separator of the card, with a second launcher on both backends
            for backend in RULE4_BACKENDS:
                self.assertTrue(any(sep in t and f"-Backend {backend}." in t for t in RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS),
                                f"no row for separator {sep!r} with backend {backend}")
        rows = list(RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "rule-4 second-launcher rows the lint passed that it must refuse")

    def test_green_rule4_both_launchers_venue_routed_pass(self):
        rows = list(RULE4_BOTH_ROUTED_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "rule-4 rows with both launchers routed that the lint refused")

    def test_red_rule4_r2_a_launcher_never_reaches_across_a_clause_into_the_next_launcher(self):
        """r2 (CoS review of 75cb00e1): the two CoS repro lines, every cross-clause variant after every separator (` or ` included), the
        route that belongs to launcher B only, and the mixed-style pairs are refused."""
        cos = f"tools/profiling/{SMOKE_SCRIPT}"
        for line in (f"pwsh -File build.ps1 on Bachelor; pwsh -File {cos} -Backend cpu.",
                     f"{APP} take01.dat on Bachelor; then run {APP} take01.dat {SMOKE_MODE}."):
            self.assertIn(line, RULE4_R2_CROSS_RED)
        self.assertTrue(any(" or run " in t for t in RULE4_R2_CROSS_REFUSE + RULE4_R2_OR_REFUSE + RULE4_R2_MIXED_REFUSE), "no ` or ` separator row")
        rows = list(RULE4_R2_REFUSE + RULE4_R2_GUARDS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r2-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r2 rows the lint passed that it must refuse")

    def test_green_rule4_r2_routed_launchers_pass(self):
        """r2: every launcher routed (cross-clause, ` or `, single-launcher clauses, route on B with a non-launcher A, mixed styles)."""
        rows = list(RULE4_R2_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r2-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r2 rows with every launcher routed that the lint refused")

    def test_rule4_r2_the_cross_clause_rows_are_red_on_the_r1_head_and_every_other_row_keeps_its_r1_verdict(self):
        """RED proof kept in the suite: the CoS repro shapes (RULE4_R2_CROSS_RED) PASS on the lint of the r1 head 75cb00e1. Nothing else
        moves against it except the listed opening: a route on launcher B whose lead A is NOT a launcher (RULE4_R2_B_PASS) was refused
        at the r1 head (the span swallowed B and took A's clause) and passes now. The r1 sets keep their 98 / 196 rows and verdicts."""
        self.maxDiff = None
        r1 = _rule4_r1_head_lint()
        (self.tmp / "rule4-r1-head.ps1").write_text(r1[1], encoding="utf-8")
        self.assertEqual((98, 196), (len(RULE4_SECOND_LAUNCHER_REFUSE), len(RULE4_BOTH_ROUTED_PASS)))
        red = list(RULE4_R2_CROSS_RED)
        refuse_rows = list(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS)
        pass_rows = list(RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS)
        texts = refuse_rows + pass_rows
        # one pass over every row on each lint, both lints at once (the RED rows are among the refuse rows, so they cost nothing extra)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r1-head.ps1", red + texts, "rule4-r2-r1head"), (LINT_PS1, texts, "rule4-r2-now")])
        counts = _json_counts(self.tmp / "rule4-r1-head.ps1", red, self.tmp, "rule4-r2-red")
        self.assertEqual([], [t for t, n in zip(red, counts) if n > 0], f"rows {r1[0]} already refuses: not a RED row")
        old = _json_counts(self.tmp / "rule4-r1-head.ps1", texts, self.tmp, "rule4-r2-r1head")
        new = _json_counts(LINT_PS1, texts, self.tmp, "rule4-r2-now")
        closed = [t for t, a, b in zip(texts, old, new) if a == 0 and b > 0]
        opened = [t for t, a, b in zip(texts, old, new) if a > 0 and b == 0]
        self.assertEqual([], [t for t in closed if t not in RULE4_R2_REFUSE], f"a row {r1[0]} passed that is not a listed r2 closure")
        self.assertEqual([], [t for t in red if t not in closed], "every RED row must close")
        # r4 (CoS / Iverson Improvement on 4b9c9387): the opening is pinned to its exact rows, not to a non-empty subset of B_PASS
        self.assertEqual(sorted(RULE4_R2_B_OPENED), sorted(opened), "the route-on-B rows must be exactly the over-refusal r2 removed")
        self.assertEqual(3, len(RULE4_R2_B_OPENED))

    def test_red_rule4_r4_quoted_calls_and_path_arguments_refuse(self):
        """r4: sol's quoted second launcher after every separator, the quoted app call, and Iverson's path-argument rows refuse."""
        script = f"tools/profiling/{SMOKE_SCRIPT}"
        first = f"The venue job calls {script} -Backend cpu on Bachelor"
        for sep in ("; ", ", then ", " then ", ". Then ", "\n"):
            self.assertIn(f"{first}{sep}& '{script}' -Backend cuda.", RULE4_R4_RED)
        rows = list(RULE4_R4_REFUSE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r4-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r4 rows the lint passed that it must refuse")

    def test_green_rule4_r4_routed_quoted_calls_and_path_arguments_pass(self):
        rows = list(RULE4_R4_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r4-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r4 rows with every launcher routed that the lint refused")

    def test_rule4_r4_rows_are_red_on_the_r3_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: every RULE4_R4_RED row PASSES on the lint of the r3 head 4b9c9387 (and every guard is refused
        there). Against that head nothing else moves: every r1 / r2 row keeps its verdict."""
        self.maxDiff = None
        r3 = _rule4_r1_head_lint(RULE4_R3_PR_HEAD, "r3")
        (self.tmp / "rule4-r3-head.ps1").write_text(r3[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS))
        mine = list(RULE4_R4_REFUSE + RULE4_R4_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r3-head.ps1", earlier + mine, "rule4-r4-r3head"), (LINT_PS1, earlier + mine, "rule4-r4-now")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r3-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        self.assertEqual([], [t for t in RULE4_R4_RED if old[t] > 0], f"rows {r3[0]} already refuses: not a RED row")
        self.assertEqual([], [t for t in RULE4_R4_GUARDS if old[t] == 0], f"guard rows {r3[0]} passes: they belong in the RED rows")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)], f"an r1 / r2 row changed its verdict against {r3[0]}")

    def test_red_rule4_r6_a_clip_after_an_option_or_a_quoted_spaced_path_is_a_launch_of_its_own(self):
        """r6: the CoS rows (B1, Improvement 1, B2) and the clip-argument axis (option forms, no extension, quoted with and without a space,
        as the first and as the second launcher, after every separator) refuse; the same rows with their own route pass."""
        for row in RULE4_R6_NAMED:
            self.assertIn(row, RULE4_R6_REFUSE)
        self.assertEqual(10, len(RULE4_R6_NAMED))
        self.assertTrue(any("'D:\\my clips" in t for t in RULE4_R6_RED) and any('"D:\\my clips' in t for t in RULE4_R6_RED), "the quoted-with-space axis rows")
        rows = list(RULE4_R6_REFUSE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r6-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r6 rows the lint passed that it must refuse")
        rows = list(RULE4_R6_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r6-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r6 rows with every launcher routed that the lint refused")

    def test_rule4_r6_rows_are_red_on_the_r4b_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: every RULE4_R6_RED row PASSES on the lint of the r4b head 469e513d (and every guard is refused
        there). Against that head nothing else moves: every earlier rule-4 row keeps its verdict."""
        self.maxDiff = None
        r4b = _rule4_r1_head_lint(RULE4_R4B_PR_HEAD, "r4b")
        (self.tmp / "rule4-r4b-head.ps1").write_text(r4b[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS + RULE4_R4_REFUSE + RULE4_R4_PASS))
        mine = list(RULE4_R6_REFUSE + RULE4_R6_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r4b-head.ps1", earlier + mine, "rule4-r6-r4bhead"), (LINT_PS1, earlier + mine, "rule4-r6-now")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r4b-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        self.assertEqual([], [t for t in RULE4_R6_RED if old[t] > 0], f"rows {r4b[0]} already refuses: not a RED row")
        self.assertEqual([], [t for t in RULE4_R6_GUARDS if old[t] == 0], f"guard rows {r4b[0]} passes: they belong in the RED rows")
        self.assertEqual([], [t for t in RULE4_R6_PASS if old[t] > 0], f"routed rows {r4b[0]} refuses: the ALLOW rows must be unchanged")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)], f"an earlier rule-4 row changed its verdict against {r4b[0]}")

    def test_red_rule4_r7_any_first_argument_after_the_app_is_a_launch_of_its_own(self):
        """r7: the CoS rows (B3 with its `; run` / `, then run` twins, N1) and the generated argument-grammar sweep (attached `=` values,
        `/` options, bare tokens, numbers, quoted values, option chains of 1-8, first and second launcher, every separator and launcher
        twin) refuse without a route of their own; the same rows with their own route pass."""
        self.assertEqual(13, len(RULE4_R7_NAMED))
        self.assertEqual(12, len(RULE4_R7_NAMED_B3))
        for row in RULE4_R7_NAMED:
            self.assertIn(row, RULE4_R7_REFUSE)
        args = _rule4_r7_args()
        self.assertTrue(any("=" in a for a in args) and any(a.startswith("/") for a in args) and "take01" in args, "the B3 argument forms")
        self.assertTrue(any(a.count("--o") == 8 for a in args), "the option chain of eight")
        self.assertEqual((913, 912), (len(RULE4_R7_REFUSE), len(RULE4_R7_PASS)))
        rows = list(RULE4_R7_REFUSE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r7-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r7 rows the lint passed that it must refuse")
        rows = list(RULE4_R7_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r7-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r7 rows with every launcher routed that the lint refused")

    def test_rule4_r7_sweep_no_row_the_pre_change_master_refuses_passes_on_the_head(self):
        """The Prevention check: the rows are GENERATED from an argument grammar and judged against the pinned pre-change master, not against
        a list of shapes. No row that RULE4_PRE_CHANGE_MASTER refuses may pass on the head unless RULE4_R7_SWEEP_ALLOWED names it (it is
        empty). It does not skip once master carries this lint: the property stays true after the merge."""
        self.maxDiff = None
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r7-pin.ps1").write_text(pin[1], encoding="utf-8")
        texts = list(RULE4_R7_REFUSE + RULE4_R7_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r7-pin.ps1", texts, "rule4-r7-pin"), (LINT_PS1, texts, "rule4-r7-now")])
        old = dict(zip(texts, _verdicts(self.tmp / "rule4-r7-pin.ps1", texts)))
        new = dict(zip(texts, _verdicts(LINT_PS1, texts)))
        opened = [t for t in texts if old[t] > 0 and new[t] == 0 and t not in RULE4_R7_SWEEP_ALLOWED]
        self.assertEqual([], opened, f"rows {pin[0]} refuses that now pass: an unlisted refuse -> pass flip of the argument grammar")
        self.assertEqual([], [t for t, why in RULE4_R7_SWEEP_ALLOWED.items() if not why.strip()], "a sweep allow-list entry has no reason")
        self.assertEqual([], [t for t in RULE4_R7_PASS if old[t] > 0], f"routed rows {pin[0]} refuses: the ALLOW rows must be unchanged")
        # the B3 rows are the ones the pin refuses and a list-based head would pass: they are in the sweep, so the sweep covers them
        for row in RULE4_R7_NAMED_B3:
            self.assertGreater(old[row], 0, f"{pin[0]} passes the B3 row: it is not a refuse -> pass flip")

    def test_rule4_r7_rows_are_red_on_the_r6_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: the four B3 rows (and their twins) and N1 PASS on the lint of the r6 head 0fb1eb8c, and so do other
        sweep rows (option chains past six, attached values, `/` options, bare tokens). Against that head nothing else moves: the routed
        rows keep passing and every earlier rule-4 row keeps its verdict."""
        self.maxDiff = None
        r6 = _rule4_r1_head_lint(RULE4_R6_PR_HEAD, "r6")
        (self.tmp / "rule4-r6-head.ps1").write_text(r6[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS + RULE4_R4_REFUSE + RULE4_R4_PASS + RULE4_R6_REFUSE + RULE4_R6_PASS))
        mine = list(RULE4_R7_REFUSE + RULE4_R7_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r6-head.ps1", earlier + mine, "rule4-r7-r6head"), (LINT_PS1, earlier + mine, "rule4-r7-r6now")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r6-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        for row in RULE4_R7_NAMED:
            self.assertEqual(0, old[row], f"{r6[0]} already refuses a CoS row: not a RED row")
        red = [t for t in RULE4_R7_REFUSE if old[t] == 0]
        self.assertGreater(len(red), len(RULE4_R7_NAMED), "the sweep reaches past the named rows")
        self.assertTrue(any("--o7" in t for t in red) and any("=take01" in t or "=D:" in t for t in red) and any("/clip" in t for t in red), "the N2 / B3 argument forms are RED")
        self.assertEqual([], [t for t in RULE4_R7_PASS if old[t] > 0], f"routed rows {r6[0]} refuses: the ALLOW rows must be unchanged")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)], f"an earlier rule-4 row changed its verdict against {r6[0]}")

    def test_red_rule4_r8_the_lead_of_a_launch_is_any_run_of_openers_and_plain_words(self):
        """r8: the opus rows (a markdown code span, `Re-run`, a word between the verb and the app; each with its `; run` / `, then run`
        twins) and the generated lead-wrapper x verb-form sweep refuse without a route of their own; the same rows with their own route
        pass. The accepted over-refusal (a harmless second call after a routed launch) refuses."""
        self.assertEqual(12, len(RULE4_R8_NAMED))
        for row in RULE4_R8_NAMED:
            self.assertIn(row, RULE4_R8_REFUSE)
        self.assertTrue(any("`" in t for t in RULE4_R8_NAMED) and any(t.startswith("Re-run ") for t in RULE4_R8_NAMED)
                        and any(t.startswith("Run the ") for t in RULE4_R8_NAMED), "the opus row shapes")
        self.assertEqual({"Run", "run", "Open", "open", "Launch", "Start", "Re-run", "Then run"}, set(RULE4_R8_VERBS))
        rows = list(RULE4_R8_REFUSE + RULE4_R8_OVERREFUSAL)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r8-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r8 rows the lint passed that it must refuse")
        rows = list(RULE4_R8_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r8-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r8 rows with every launcher routed that the lint refused")

    def test_rule4_r8_sweep_no_row_the_pre_change_master_refuses_passes_on_the_head(self):
        """The r8 Prevention check: the lead-wrapper x verb-form rows are GENERATED and judged against the pinned pre-change master. No row
        it refuses may pass on the head unless RULE4_R8_SWEEP_ALLOWED names it (empty). It does not skip once master carries this lint."""
        self.maxDiff = None
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r8-pin.ps1").write_text(pin[1], encoding="utf-8")
        texts = list(RULE4_R8_REFUSE + RULE4_R8_PASS + RULE4_R8_OVERREFUSAL)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r8-pin.ps1", texts, "rule4-r8-pin"), (LINT_PS1, texts, "rule4-r8-now")])
        old = dict(zip(texts, _verdicts(self.tmp / "rule4-r8-pin.ps1", texts)))
        new = dict(zip(texts, _verdicts(LINT_PS1, texts)))
        opened = [t for t in texts if old[t] > 0 and new[t] == 0 and t not in RULE4_R8_SWEEP_ALLOWED]
        self.assertEqual([], opened, f"rows {pin[0]} refuses that now pass: an unlisted refuse -> pass flip of the lead grammar")
        self.assertEqual([], [t for t, why in RULE4_R8_SWEEP_ALLOWED.items() if not why.strip()], "a sweep allow-list entry has no reason")
        self.assertEqual([], [t for t in RULE4_R8_PASS if old[t] > 0], f"routed rows {pin[0]} refuses: the ALLOW rows must be unchanged")
        for row in RULE4_R8_NAMED:
            self.assertGreater(old[row], 0, f"{pin[0]} passes the opus row: it is not a refuse -> pass flip")

    def test_rule4_r8_rows_are_red_on_the_r7_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: the opus rows PASS on the lint of the r7 head 03e7ae1b, and so do other sweep rows (wrapped and
        prefixed leads). Against that head nothing else moves: the routed rows keep passing and every earlier rule-4 row keeps its verdict."""
        self.maxDiff = None
        r7 = _rule4_r1_head_lint(RULE4_R7_PR_HEAD, "r7")
        (self.tmp / "rule4-r7-head.ps1").write_text(r7[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS + RULE4_R4_REFUSE + RULE4_R4_PASS + RULE4_R6_REFUSE + RULE4_R6_PASS
                                     + RULE4_R7_REFUSE + RULE4_R7_PASS))
        mine = list(RULE4_R8_REFUSE + RULE4_R8_PASS + RULE4_R8_OVERREFUSAL)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r7-head.ps1", earlier + mine, "rule4-r8-r7head"), (LINT_PS1, earlier + mine, "rule4-r8-r7now")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r7-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        for row in RULE4_R8_NAMED:
            self.assertEqual(0, old[row], f"{r7[0]} already refuses an opus row: not a RED row")
        red = [t for t in RULE4_R8_REFUSE if old[t] == 0]
        self.assertGreater(len(red), len(RULE4_R8_NAMED), "the sweep reaches past the named rows")
        self.assertTrue(any("`" in t for t in red) and any("Re-run" in t for t in red) and any(" the " in t for t in red), "the lead-wrapper and verb-form axes are RED")
        self.assertEqual([], [t for t in RULE4_R8_PASS if old[t] > 0], f"routed rows {r7[0]} refuses: the ALLOW rows must be unchanged")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)], f"an earlier rule-4 row changed its verdict against {r7[0]}")

    def test_red_rule4_r9_an_app_token_is_a_launch_decided_from_its_own_gap_whatever_stands_before_it(self):
        """r9 (CoS review 6230178238, B5; hub ruling): the five CoS families (an unquoted spaced path, an apostrophe word, a hyphenated
        word, punctuation in the lead, no verb right before the app) x six launch verbs x the three separators x two launcher twins refuse
        without a route of their own; the same rows with their own route pass. The 15 `<app> <clip><sep><routed script> on Bachelor.` rows
        (RULE4_R2_B_APP_REFUSE) are an accepted over-refusal and refuse: the first clause is an app launch with a clip and no route."""
        self.assertEqual(27, len(RULE4_R9_NAMED))
        for row in RULE4_R9_NAMED:
            self.assertIn(row, RULE4_R9_REFUSE)
        self.assertEqual({1, 2, 3, 4, 5}, set(RULE4_R9_FAMILY.values()), "the five CoS families")
        self.assertEqual(15, len(RULE4_R2_B_APP_REFUSE))
        rows = list(RULE4_R9_REFUSE + RULE4_R2_B_APP_REFUSE)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r9-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0], "r9 rows the lint passed that it must refuse")
        rows = list(RULE4_R9_PASS)
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r9-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0], "r9 rows with every launcher routed that the lint refused")

    def test_rule4_r9_sweep_no_row_the_pre_change_master_refuses_passes_on_the_head(self):
        """The r9 sweep against the PINNED pre-change master with an EMPTY allow-list: the CoS families, their routed twins, the 15 named
        over-refusal rows and the seeded lead fuzz (RULE4_R9_FUZZ_ROWS rows, seed RULE4_R9_FUZZ_SEED). No row the pin refuses may pass on
        the head. It does not skip once master carries this lint."""
        self.maxDiff = None
        self.assertGreaterEqual(len(RULE4_R9_FUZZ_REFUSE), 3000, "the fuzz is at least a few thousand rows")
        self.assertEqual(len(RULE4_R9_FUZZ_REFUSE), len(set(RULE4_R9_FUZZ_REFUSE)))
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r9-pin.ps1").write_text(pin[1], encoding="utf-8")
        texts = list(RULE4_R9_REFUSE + RULE4_R9_PASS + RULE4_R2_B_APP_REFUSE + RULE4_R9_FUZZ_REFUSE + RULE4_R9_FUZZ_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r9-pin.ps1", texts, "rule4-r9-pin"), (LINT_PS1, texts, "rule4-r9-now")])
        old = dict(zip(texts, _verdicts(self.tmp / "rule4-r9-pin.ps1", texts)))
        new = dict(zip(texts, _verdicts(LINT_PS1, texts)))
        opened = [t for t in texts if old[t] > 0 and new[t] == 0 and t not in RULE4_R9_SWEEP_ALLOWED]
        self.assertEqual([], opened[:20], f"{len(opened)} row(s) {pin[0]} refuses that now pass: an unlisted refuse -> pass flip of the lead")
        self.assertEqual([], [t for t, why in RULE4_R9_SWEEP_ALLOWED.items() if not why.strip()], "a sweep allow-list entry has no reason")
        self.assertEqual([], [t for t in RULE4_R9_PASS + RULE4_R9_FUZZ_PASS if old[t] > 0], f"routed rows {pin[0]} refuses: the ALLOW rows must be unchanged")
        for row in RULE4_R9_NAMED:
            self.assertGreater(old[row], 0, f"{pin[0]} passes a CoS row: it is not a refuse -> pass flip")
        # the fuzz refuses by construction: the first call has no route of its own, whatever stands in front of it
        self.assertEqual([], [t for t in RULE4_R9_FUZZ_REFUSE if new[t] == 0][:20], "fuzz rows with no route of their own that the head passes")
        self.assertEqual([], [t for t in RULE4_R9_FUZZ_PASS if new[t] > 0][:20], "fuzz rows with a route on every launcher that the head refuses")

    def test_rule4_r9_rows_are_red_on_the_r8_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: the CoS rows, the 15 named rows and a large part of the fuzz PASS on the lint of the r8 head
        d30d1365 (and so does every family). Against that head nothing else moves: the routed rows keep passing and every earlier
        rule-4 row keeps its verdict."""
        self.maxDiff = None
        r8 = _rule4_r1_head_lint(RULE4_R8_PR_HEAD, "r8")
        (self.tmp / "rule4-r8-head.ps1").write_text(r8[1], encoding="utf-8")
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r9-pin2.ps1").write_text(pin[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS + RULE4_R4_REFUSE + RULE4_R4_PASS + RULE4_R6_REFUSE + RULE4_R6_PASS
                                     + RULE4_R7_REFUSE + RULE4_R7_PASS + RULE4_R8_REFUSE + RULE4_R8_PASS + RULE4_R8_OVERREFUSAL))
        mine = list(RULE4_R9_REFUSE + RULE4_R9_PASS + RULE4_R2_B_APP_REFUSE + RULE4_R9_FUZZ_REFUSE + RULE4_R9_FUZZ_PASS)
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r8-head.ps1", earlier + mine, "rule4-r9-r8head"), (LINT_PS1, earlier + mine, "rule4-r9-r8now"),
                                    (self.tmp / "rule4-r9-pin2.ps1", mine, "rule4-r9-pin2")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r8-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        pinned = dict(zip(mine, _verdicts(self.tmp / "rule4-r9-pin2.ps1", mine)))
        for row in RULE4_R9_NAMED + RULE4_R2_B_APP_REFUSE:
            self.assertEqual(0, old[row], f"{r8[0]} already refuses a CoS / named row: not a RED row")
        red = [t for t in RULE4_R9_REFUSE if old[t] == 0]
        self.assertEqual({1, 2, 3, 4, 5}, {RULE4_R9_FAMILY[t] for t in red if t in RULE4_R9_FAMILY}, "every CoS family has RED rows")
        fuzz_red = [t for t in RULE4_R9_FUZZ_REFUSE if old[t] == 0 and pinned[t] > 0]
        self.assertGreaterEqual(len(fuzz_red), 100, f"the fuzz reaches the lead holes: {len(fuzz_red)} row(s) the pin refuses and {r8[0]} passes")
        self.assertEqual([], [t for t in RULE4_R9_PASS + RULE4_R9_FUZZ_PASS if old[t] > 0], f"routed rows {r8[0]} refuses: the ALLOW rows must be unchanged")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)][:20], f"an earlier rule-4 row changed its verdict against {r8[0]}")

    def test_red_rule4_r10_a_semicolon_between_the_app_argument_and_its_mode_never_hides_the_launch(self):
        """r10 (CoS review 6230713095, B6; hub ruling): an app call with an argument is scored whenever the sentence holds a playback mode
        flag anywhere, so `<app> <clip>; add <mode>.` must carry its own route. The eight CoS rows, the tail grid (what stands between the
        app's argument and the mode: `;`, `; then`, `;` with no space, ` ; `, a later clause with a verb, a negated later clause, a later
        routed run), the no-argument form (the soft note) and the tail-axis fuzz refuse; the same calls with their own route pass; a
        route glued to the `;` (`on Bachelor;add`) is no route and refuses."""
        self.assertEqual(8, len(RULE4_R10_NAMED))
        self.assertGreaterEqual(len(RULE4_R10_FUZZ_REFUSE), 2000, "the tail fuzz is at least a couple of thousand rows")
        self.assertEqual(len(RULE4_R10_FUZZ_REFUSE), len(set(RULE4_R10_FUZZ_REFUSE)))
        self.assertTrue(RULE4_R10_GLUED_ROUTE and RULE4_R10_NOARG and RULE4_R10_GRID_ROUTED)
        rows = list(dict.fromkeys(RULE4_R10_REFUSE + RULE4_R10_FUZZ_REFUSE + RULE4_R10_GLUED_ROUTE))
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r10-refuse")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n == 0][:20], "r10 rows the lint passed that it must refuse")
        rows = list(dict.fromkeys(RULE4_R10_GRID_ROUTED + RULE4_R10_NOARG_ROUTED + RULE4_R10_FUZZ_PASS + RULE4_R10_MENTION_PASS))
        counts = _json_counts(LINT_PS1, rows, self.tmp, "rule4-r10-pass")
        self.assertEqual([], [t for t, n in zip(rows, counts) if n > 0][:20],
                         "r10 rows with a route on the app call (or a bare app mention before a routed launch) that the lint refused")

    def test_rule4_r10_sweep_no_row_the_pin_or_master_refuses_passes_on_the_head(self):
        """The r10 sweep against the PINNED pre-change master (and against the current baseline ref, when it still differs from the head)
        with an EMPTY allow-list: the CoS rows, the tail grid, the no-argument form, the routed and glued twins, the 15 named over-refusal
        rows and the tail-axis fuzz (RULE4_R10_FUZZ_ROWS rows, seed RULE4_R10_FUZZ_SEED). No row the pin refuses may pass on the head. It
        does not skip once master carries this lint."""
        self.maxDiff = None
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r10-pin.ps1").write_text(pin[1], encoding="utf-8")
        texts = list(dict.fromkeys(RULE4_R10_REFUSE + RULE4_R10_GRID_ROUTED + RULE4_R10_GLUED_ROUTE + RULE4_R10_NOARG_ROUTED + RULE4_R10_MENTION_PASS
                                   + RULE4_R2_B_APP_REFUSE + RULE4_R10_FUZZ_REFUSE + RULE4_R10_FUZZ_PASS))
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r10-pin.ps1", texts, "rule4-r10-pin"), (LINT_PS1, texts, "rule4-r10-now")])
        old = dict(zip(texts, _verdicts(self.tmp / "rule4-r10-pin.ps1", texts)))
        new = dict(zip(texts, _verdicts(LINT_PS1, texts)))
        opened = [t for t in texts if old[t] > 0 and new[t] == 0 and t not in RULE4_R10_SWEEP_ALLOWED]
        self.assertEqual([], opened[:20], f"{len(opened)} row(s) {pin[0]} refuses that now pass: an unlisted refuse -> pass flip of the tail")
        self.assertEqual([], [t for t, why in RULE4_R10_SWEEP_ALLOWED.items() if not why.strip()], "a sweep allow-list entry has no reason")
        self.assertEqual([], [t for t in RULE4_R10_GRID_ROUTED + RULE4_R10_NOARG_ROUTED + RULE4_R10_MENTION_PASS + RULE4_R10_FUZZ_PASS if old[t] > 0][:20],
                         f"routed rows {pin[0]} refuses: the ALLOW rows must be unchanged")
        for row in RULE4_R10_NAMED + RULE4_R10_GRID:
            self.assertGreater(old[row], 0, f"{pin[0]} passes a CoS / grid row: it is not a refuse -> pass flip")
        # a route glued to the `;` is no route on the pin either: the head agrees with it row for row
        self.assertEqual([], [t for t in RULE4_R10_GLUED_ROUTE if (old[t] > 0) != (new[t] > 0)][:20], "glued-route rows where the pin and the head disagree")
        # the no-argument form is a tightening: the head refuses every row, the pin does not
        self.assertTrue(any(old[t] == 0 for t in RULE4_R10_NOARG), "the no-argument tightening is a flip against the pin (the pin passes some of it)")
        # the fuzz refuses by construction: the app call has no route of its own, whatever stands in front of it or after it
        self.assertEqual([], [t for t in RULE4_R10_FUZZ_REFUSE if new[t] == 0][:20], "tail fuzz rows with no route of their own that the head passes")
        base = _baseline_lint_source()
        if base is not None and base[1].replace("\r\n", "\n") != LINT_PS1.read_text(encoding="utf-8").replace("\r\n", "\n"):
            (self.tmp / "rule4-r10-master.ps1").write_text(base[1], encoding="utf-8")
            rows = list(dict.fromkeys(RULE4_R10_NAMED + RULE4_R10_GRID + RULE4_R10_GRID_ROUTED))
            _prefetch_counts(self.tmp, [(self.tmp / "rule4-r10-master.ps1", rows, "rule4-r10-master")])
            master = dict(zip(rows, _verdicts(self.tmp / "rule4-r10-master.ps1", rows)))
            self.assertEqual([], [t for t in rows if master[t] > 0 and new[t] == 0][:20], f"rows {base[0]} refuses that now pass")

    def test_rule4_r10_rows_are_red_on_the_r9_head_and_every_earlier_row_keeps_its_verdict(self):
        """RED proof kept in the suite: the CoS rows, the tail grid, the no-argument form and the tail fuzz all PASS on the lint of the r9
        head 96cdd148 (the app gap stops at `;`, the sentence holds no direct launch, nothing is scored), and the pin refuses the CoS
        rows. Against that head nothing else moves: the routed rows keep passing and every earlier rule-4 row keeps its verdict."""
        self.maxDiff = None
        r9 = _rule4_r1_head_lint(RULE4_R9_PR_HEAD, "r9")
        (self.tmp / "rule4-r9-head.ps1").write_text(r9[1], encoding="utf-8")
        pin = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master", moot_once_master_carries_it=False)
        (self.tmp / "rule4-r10-pin2.ps1").write_text(pin[1], encoding="utf-8")
        earlier = list(dict.fromkeys(RULE4_R2_REFUSE + RULE4_R2_GUARDS + RULE4_SECOND_LAUNCHER_REFUSE + RULE4_SECOND_LAUNCHER_GUARDS
                                     + RULE4_R2_PASS + RULE4_BOTH_ROUTED_PASS + RULE4_R4_REFUSE + RULE4_R4_PASS + RULE4_R6_REFUSE + RULE4_R6_PASS
                                     + RULE4_R7_REFUSE + RULE4_R7_PASS + RULE4_R8_REFUSE + RULE4_R8_PASS + RULE4_R8_OVERREFUSAL
                                     + RULE4_R9_REFUSE + RULE4_R9_PASS + RULE4_R2_B_APP_REFUSE + RULE4_R9_FUZZ_REFUSE + RULE4_R9_FUZZ_PASS))
        mine = list(dict.fromkeys(RULE4_R10_REFUSE + RULE4_R10_GRID_ROUTED + RULE4_R10_GLUED_ROUTE + RULE4_R10_NOARG_ROUTED + RULE4_R10_MENTION_PASS
                                  + RULE4_R10_FUZZ_REFUSE + RULE4_R10_FUZZ_PASS))
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-r9-head.ps1", earlier + mine, "rule4-r10-r9head"), (LINT_PS1, earlier + mine, "rule4-r10-r9now"),
                                    (self.tmp / "rule4-r10-pin2.ps1", mine, "rule4-r10-pin2")])
        old = dict(zip(earlier + mine, _verdicts(self.tmp / "rule4-r9-head.ps1", earlier + mine)))
        new = dict(zip(earlier + mine, _verdicts(LINT_PS1, earlier + mine)))
        pinned = dict(zip(mine, _verdicts(self.tmp / "rule4-r10-pin2.ps1", mine)))
        for row in RULE4_R10_NAMED + RULE4_R10_NOARG:
            self.assertEqual(0, old[row], f"{r9[0]} already refuses a CoS / no-argument row: not a RED row")
        for row in RULE4_R10_NAMED:
            self.assertGreater(pinned[row], 0, f"{pin[0]} passes a CoS row: it would be a tightening, not a parity restore")
        self.assertEqual([], [t for t in RULE4_R10_GRID if old[t] > 0][:20], f"grid rows {r9[0]} already refuses: not RED rows")
        fuzz_red = [t for t in RULE4_R10_FUZZ_REFUSE if old[t] == 0 and pinned[t] > 0]
        self.assertGreaterEqual(len(fuzz_red), 1000, f"the tail fuzz reaches the hole: {len(fuzz_red)} row(s) the pin refuses and {r9[0]} passes")
        self.assertEqual([], [t for t in RULE4_R10_FUZZ_REFUSE if old[t] > 0][:20], f"tail fuzz rows {r9[0]} already refuses")
        self.assertEqual([], [t for t in RULE4_R10_GRID_ROUTED + RULE4_R10_NOARG_ROUTED + RULE4_R10_MENTION_PASS + RULE4_R10_FUZZ_PASS if old[t] > 0],
                         f"routed rows {r9[0]} refuses: the ALLOW rows must be unchanged")
        self.assertEqual([], [t for t in RULE4_R10_GLUED_ROUTE if old[t] > 0], f"glued-route rows {r9[0]} refuses: they open only because of the hole")
        self.assertEqual([], [t for t in earlier if (old[t] > 0) != (new[t] > 0)][:20], f"an earlier rule-4 row changed its verdict against {r9[0]}")

    def test_rule4_every_quote_class_holds_every_character_its_comment_claims(self):
        """r9 (CoS Prevention, the B4 root cause): the EVALUATED patterns, not their source, hold the quote characters their comments claim.
        Static guard: evaluated-only for the delimiter escape (see RULE4_BACKTICK_ESCAPES), because a backtick before `"` is also the one
        legitimate escape in a double-quoted pattern (six strings use it), so the source form cannot tell a meant backtick from an escape;
        every other single backtick before a bracket or an apostrophe in a double-quoted string is refused unless named."""
        self.assertEqual([], _quote_class_problems(LINT_PS1, RULE4_QUOTE_CLAIMS, self.tmp, "quote-now"))
        literals = _lint_pattern_probe(LINT_PS1, ("appStop",), self.tmp, "quote-static")["dq"]
        self.assertGreater(len(literals), 20, "the double-quoted literals of the lint were read")
        stray = [(lit[:60], hit) for lit in literals for hit in _single_backticks_before_bracket_or_quote(lit)
                 if lit not in RULE4_BACKTICK_ESCAPES]
        self.assertEqual([], stray, "a single backtick before [ ] ( ) or ' inside a double-quoted pattern string escapes that character")
        self.assertEqual([], [k for k, why in RULE4_BACKTICK_ESCAPES.items() if not why.strip()], "a named backtick escape has no reason")

    def test_rule4_the_quote_class_pin_bites_on_a_dropped_backtick_and_a_stray_escape(self):
        """RED/GREEN proof kept in the suite: the r7 head 03e7ae1b (its app-token classes lack the backtick the r8 comment claims) fails the
        pin; the r8 head d30d1365 (the fix, with the lead pieces) passes it; the static scanner flags a single backtick before a bracket
        or an apostrophe and nothing else."""
        r7 = _rule4_r1_head_lint(RULE4_R7_PR_HEAD, "r7")
        (self.tmp / "quote-r7.ps1").write_text(r7[1], encoding="utf-8")
        problems = _quote_class_problems(self.tmp / "quote-r7.ps1", {k: v for k, v in RULE4_QUOTE_CLAIMS.items() if k in ("appStop", "clipLaunch")}, self.tmp, "quote-r7")
        self.assertTrue(any(p.startswith("appStop: class") for p in problems) and any(p.startswith("clipLaunch: class") for p in problems),
                        f"{r7[0]} must fail the pin on the dropped backtick of both app-token patterns: {problems}")
        r8 = _rule4_r1_head_lint(RULE4_R8_PR_HEAD, "r8")
        (self.tmp / "quote-r8.ps1").write_text(r8[1], encoding="utf-8")
        self.assertEqual([], _quote_class_problems(self.tmp / "quote-r8.ps1", RULE4_QUOTE_CLAIMS_R8, self.tmp, "quote-r8"))
        self.assertEqual(["`'"], _single_backticks_before_bracket_or_quote('"[a`\'b]"'))
        self.assertEqual(["`]"], _single_backticks_before_bracket_or_quote('"[a`]"'))
        self.assertEqual([], _single_backticks_before_bracket_or_quote('"[&(\'`"``]*"'))
        self.assertEqual([], _single_backticks_before_bracket_or_quote('"``[x]"'))

    def test_rule4_the_refuse_rows_are_red_on_the_baseline(self):
        """RED proof kept in the suite: every second-launcher row passes the lint of RULE4_PRE_CHANGE_MASTER, the master this PR branched
        from (its rule 4 read only the first launcher occurrence).

        r4b (CoS Improvement 6224545625): the proof is judged against that PINNED pre-change commit, never against fork/master or
        HUB_VM_LINT_BASELINE_REF. Once this PR merges those refs carry the change, so the next lint PR (whose lint differs from master's
        by one byte) failed here with 'already refuses: not a RED row'; only byte-identity used to skip. If the pinned baseline ever
        refuses EVERY RED row it carries the change and is no RED baseline: the test skips with that reason instead of failing. The pin
        never changes, so that skip can only fire in a broken clone or after a history rewrite (CoS nit on 469e513d); it is a guard, not a path.

        Retirement: the pin never moves (re-pinning it to a later master would make it carry the change). The test is deleted, not
        re-pinned, when the rule-4 catalogue retires; the candidate verdicts stay pinned by test_red_rule4_* either way."""
        self.maxDiff = None
        base = _rule4_r1_head_lint(RULE4_PRE_CHANGE_MASTER, "pre-change master")
        (self.tmp / "rule4-master.ps1").write_text(base[1], encoding="utf-8")
        rows = list(RULE4_SECOND_LAUNCHER_REFUSE)
        # one pwsh pass over the refuse, guard and allow rows together
        _prefetch_counts(self.tmp, [(self.tmp / "rule4-master.ps1", rows + list(RULE4_SECOND_LAUNCHER_GUARDS) + list(RULE4_BOTH_ROUTED_PASS), "rule4-master")])
        counts = _json_counts(self.tmp / "rule4-master.ps1", rows, self.tmp, "rule4-red")
        refused = [t for t, n in zip(rows, counts) if n > 0]
        if rows and len(refused) == len(rows):
            self.skipTest(f"{base[0]} already refuses every RED row: it carries the change, so it is no RED baseline")
        self.assertEqual([], refused, f"rows {base[0]} already refuses: not a RED row, move it to the guards")
        guards = list(RULE4_SECOND_LAUNCHER_GUARDS)
        counts = _json_counts(self.tmp / "rule4-master.ps1", guards, self.tmp, "rule4-guards")
        self.assertEqual([], [t for t, n in zip(guards, counts) if n == 0], f"guard rows {base[0]} passes: they belong in the RED rows")
        allow = list(RULE4_BOTH_ROUTED_PASS)
        counts = _json_counts(self.tmp / "rule4-master.ps1", allow, self.tmp, "rule4-allow")
        self.assertEqual([], [t for t, n in zip(allow, counts) if n > 0], f"both-routed rows {base[0]} refuses: the ALLOW rows must be unchanged")

    def test_a_1mb_heading_free_prompt_completes_in_bounded_time(self):
        """r10 (opus r9 hardening HUB-VM-LINT-BLOCKTAIL-QUADRATIC-1): r9 copied the rest of the block per item, O(n^2) on a large
        heading-free prompt. ~1 MB of prose with venue-routed `locally` sentences, grammar-clean follow-ups and handling follow-ups,
        no heading anywhere, must lint in under 10 s."""
        parts: list[str] = []
        size = 0
        i = 0
        while size < 1_000_000:
            i += 1
            if i % 40 == 0:
                piece = "Run playback locally on Bachelor through the venue chain. "
            elif i % 40 in (1, 2):
                piece = "Then on Ultra-Magnus, never here. "
            else:
                piece = f"Step {i} reads the log file and records the checksum in the summary table. "
            if i % 7 == 0:
                piece += "\n"
            parts.append(piece)
            size += len(piece)
        parts.append("Finally the hub reads the summary table.\n")
        src = self.tmp / "big.txt"
        src.write_text("".join(parts), encoding="utf-8")
        probe = self.tmp / "big-probe.ps1"
        _write_probe(
            probe,
            ". " + _psq(LINT_PS1) + "\n"
            "$t = [System.IO.File]::ReadAllText(" + _psq(src) + ")\n"
            "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
            "$f = @(Get-LocalPlaybackPromptFindings -Text $t)\n"
            "$sw.Stop()\n"
            "ConvertTo-Json -InputObject @{ seconds = $sw.Elapsed.TotalSeconds; findings = $f.Count; length = $t.Length } -Compress\n",
        )
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=300)
        self.assertEqual(0, done.returncode, done.stderr)
        out = json.loads(done.stdout.strip())
        self.assertGreaterEqual(out["length"], 1_000_000)
        # every venue sentence is followed, in the same block, by prose outside the grammar: each one refuses
        self.assertEqual(sum(1 for k in range(1, i + 1) if k % 40 == 0), out["findings"])
        self.assertLess(out["seconds"], 10.0, f"a 1 MB heading-free prompt took {out['seconds']:.1f} s")

    def test_r11_a_1mb_block_of_distinct_earlier_launch_items_is_bounded_and_fails_closed(self):
        """r11b / r12: the earlier-item scan is one forward pass of the closed head grammar over the block (no memo, no cap, no per-item
        clause analysis). ~1 MB of DISTINCT items with unlisted words, no heading, then one venue-routed `locally`: it must lint in
        under 10 s and, the head being dirty, refuse that one sentence."""
        lines: list[str] = []
        size = 0
        i = 0
        while size < 1_000_000:
            i += 1
            line = f"Step {i} runs the arms on Bachelor. Step {i} records the checksum in the table. "
            lines.append(line)
            size += len(line)
        lines.append("Run playback locally on Bachelor through the venue chain.\n")
        src = self.tmp / "big-head.txt"
        src.write_text("".join(lines), encoding="utf-8")
        probe = self.tmp / "big-head-probe.ps1"
        _write_probe(
            probe,
            ". " + _psq(LINT_PS1) + "\n"
            "$t = [System.IO.File]::ReadAllText(" + _psq(src) + ")\n"
            "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
            "$f = @(Get-LocalPlaybackPromptFindings -Text $t)\n"
            "$sw.Stop()\n"
            "ConvertTo-Json -InputObject @{ seconds = $sw.Elapsed.TotalSeconds; findings = $f.Count; length = $t.Length } -Compress\n",
        )
        done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=300)
        self.assertEqual(0, done.returncode, done.stderr)
        out = json.loads(done.stdout.strip())
        self.assertGreaterEqual(out["length"], 1_000_000)
        self.assertEqual(1, out["findings"])
        self.assertLess(out["seconds"], 10.0, f"a 1 MB heading-free prompt with distinct launch items took {out['seconds']:.1f} s")

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
                  "launch-venue-control", "launch-host-destination-control",
                  "r10-adverb-insertion", "r10-adverb-insertion-cross-sentence", "r10-destination-preposition",
                  "r10-destination-preposition-cross-sentence", "r10-held-out-host", "r10-member-control", "r10-member-defeat",
                  "r10-grammar-allow", "r10-overrefusal-handling",
                  "r11-earlier-clause-held-out", "r11-earlier-clause-known", "r11-earlier-sentence", "r11-weak-negation-head",
                  "r11-earlier-group", "r11-earlier-clause-allow-tail", "r11-marker-prefix-host", "r11-overrefusal-earlier-launch",
                  "r11-allow-routed-head",
                  "r12-unlisted-verb-clause", "r12-unlisted-verb-sentence", "r12-overrefusal-unlisted-verb-routed", "r12-hyphen-prefix-form",
                  "r12-earlier-item-no-locus", "r12-allow-head", "r13-executable-as-object", "r13-allow-routed-executable"):
            self.assertIn(g, groups, f"no rows generated for {g}")
        # r13: every executable x every file argument (none included) x every clause boundary and every sentence boundary
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "r13-executable-as-object"),
                                len(self.lists["HeadExecutableWords"]) * len(R13_ARGS) * (len(R11_BOUNDARIES) + len(R11_SENTENCE_BOUNDARIES)))
        # r12: every unlisted verb x every locus x every clause boundary and every sentence boundary, and every form of every listed verb
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g.startswith("r12-") and g.endswith(("-clause", "-routed"))),
                                len(R12_UNLISTED_VERBS) * (len(R12_LOCI) - 1) * len(R11_BOUNDARIES))
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "r12-hyphen-prefix-form"),
                                # a form that is also an unlisted verb ("re-run", "relaunch") is generated once, by the unlisted-verb axis
                                len({f.format(v=v.replace(" ", "-")).lower() for v in self.lists["LaunchVerbs"] for f in R12_VERB_FORMS}
                                    - {v.replace(" ", "-").lower() for v in R12_UNLISTED_VERBS}) * 3 * 4)
        # r11b: every held-out launch verb x six held-out host nouns x every clause boundary, not a sample
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "r11-earlier-clause-held-out"),
                                len(HELD_OUT_LAUNCH_VERBS) * 6 * len(R11_BOUNDARIES))
        # r10: every destination preposition after every launch verb of the held-out pool, against every venue, in both sentence forms
        dest = len(HELD_OUT_LAUNCH_VERBS) * len(R10_DESTINATION_OBJECTS) * len(R10_DESTINATION_PREPOSITIONS) * len(R10_VENUE_POOL)
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "r10-destination-preposition"), dest)
        self.assertGreaterEqual(sum(1 for g, _, _ in self.rows if g == "r10-destination-preposition-cross-sentence"), dest)
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
        # only a bounded sample goes into the message: a hollow axis can be tens of thousands of rows
        self.assertEqual(0, len(wrong), f"{group}: {len(wrong)} of {len(rows)} rows the lint {'passed' if refuse else 'refused'} that it must "
                                        f"{'refuse' if refuse else 'pass'}, e.g. {wrong[:4]}")

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

    # r8: sentence-terminal E1. The held-out pools are TEST-ONLY and must stay disjoint from the lint's own lists, otherwise the axis
    # measures closure over the lint's vocabulary instead of fail-closed behaviour (CoS Prevention, 6th round).
    def test_r8_held_out_pools_are_disjoint_from_the_lints_lists(self):
        lint_words = {w.lower() for key in ("HostNouns", "HostNames", "HostDeterminers", "HostPrepositions", "HostLocusPrepositions",
                                            "TailLocusPrepositions", "TailNegations", "TailArticles", "TailJoiners", "TailPureLocus")
                      for w in self.lists.get(key, [])}
        for name, pool in (("HELD_OUT_HOST_NOUNS", HELD_OUT_HOST_NOUNS), ("R10_ADVERBS", R10_ADVERBS), ("HELD_OUT_LAUNCH_VERBS", HELD_OUT_LAUNCH_VERBS),
                           ("R10_LEAD_LAUNCH_VERBS", R10_LEAD_LAUNCH_VERBS), ("R10_FORMER_HANDLING_VERBS", R10_FORMER_HANDLING_VERBS)):
            self.assertEqual([], [w for w in pool if w.lower() in lint_words], f"{name} overlaps a lint list")
        # a held-out noun that merely CONTAINS a listed noun as a whole word would be read through the listed one, not held out
        listed_nouns = {w.lower() for w in self.lists["HostNouns"]}
        for w in HELD_OUT_HOST_NOUNS:
            self.assertEqual([], [p for p in w.lower().split() if p in listed_nouns], f"held-out noun {w!r} contains a listed host noun")

    def test_r10_tail_lists_are_exposed_read_by_the_lint_and_closed(self):
        """r10: the grammar's word lists are exposed and read; the r8 admit-lists that decided a tail by enumerating verbs are gone, and
        no destination preposition can enter the grammar."""
        for key in ("TailLocusPrepositions", "TailArticles", "TailJoiners", "TailNegations", "TailNegationPrepositions", "TailPureLocus"):
            self.assertTrue(self.lists.get(key), f"list {key} is missing or empty")
        text = LINT_PS1.read_text(encoding="utf-8")
        body = text[text.index("function Get-LocalPlaybackPromptFindings"):]
        for key in ("TailLocusPrepositions", "TailArticles", "TailJoiners", "TailNegations", "TailNegationPrepositions", "TailPureLocus"):
            self.assertIn(f"$lists.{key}", body, f"the findings function does not read list {key}")
        for gone in ("TailHandlingVerbs", "TailLaunchVerbs", "TailDestinationPrepositions"):
            self.assertNotIn(gone, self.lists, f"{gone} came back: r10 decides a tail by a closed grammar, not by enumerating verbs")
        self.assertEqual(["at", "on", "through", "using", "via"], sorted(self.lists["TailLocusPrepositions"]))
        self.assertEqual(["and", "or", "then"], sorted(self.lists["TailJoiners"]))
        for w in ("to", "into", "from", "for"):
            for key in ("TailLocusPrepositions", "TailNegationPrepositions", "TailJoiners", "TailArticles", "TailNegations", "TailPureLocus"):
                self.assertNotIn(w, self.lists[key], f"destination preposition {w!r} entered {key}")
        self.assertNotIn("blockTails", body, "the per-item copy of the rest of the block came back (O(n^2) on a large prompt)")

    def test_red_r10_adverb_insertion_destination_and_cross_sentence(self):
        for g in ("r10-adverb-insertion", "r10-adverb-insertion-cross-sentence", "r10-destination-preposition",
                  "r10-destination-preposition-cross-sentence"):
            self._group(g, True)

    def test_red_r10_held_out_host_nouns(self):
        self._group("r10-held-out-host", True)

    def test_red_r10_every_tail_list_member_is_defeated_outside_the_grammar(self):
        self._group("r10-member-defeat", True)
        self._group("r10-overrefusal-handling", True)

    def test_green_r10_grammar_allow_rows_and_member_controls_pass(self):
        self._group("r10-grammar-allow", False)
        self._group("r10-member-control", False)
        # the Python restatement of the grammar agrees with every ALLOW row and with no generated refusal row
        for g, refuse, text in self.rows:
            if g in ("r10-grammar-allow", "r10-member-control"):
                self.assertTrue(_e1_terminal_in_grammar(text), text)
            elif g.startswith("r10-") and refuse:
                self.assertFalse(_e1_terminal_in_grammar(text), text)

    def test_red_r11_an_earlier_launch_is_never_hidden_in_front_of_a_venue_routed_locally(self):
        for g in ("r11-earlier-clause-held-out", "r11-earlier-clause-known", "r11-earlier-sentence", "r11-weak-negation-head",
                  "r11-earlier-group", "r11-earlier-clause-allow-tail", "r11-marker-prefix-host", "r11-overrefusal-earlier-launch"):
            self._group(g, True)

    def test_green_r11_a_venue_routed_head_passes_and_the_oracle_agrees(self):
        self._group("r11-allow-routed-head", False)
        for g, refuse, text in self.rows:
            if g == "r11-allow-routed-head":
                self.assertTrue(_e1_terminal_in_grammar(text), text)
            elif g.startswith("r11-") and refuse:
                self.assertFalse(_e1_terminal_in_grammar(text), text)

    def test_red_r12_an_unlisted_verb_never_hides_in_front_of_a_venue_routed_locally(self):
        """r12: every unlisted verb and every hyphenated / prefixed form of every listed verb, x every locus (a listed host, an
        unlisted host, none, a venue) x every clause and sentence boundary, is refused."""
        for g in ("r12-unlisted-verb-clause", "r12-unlisted-verb-sentence", "r12-overrefusal-unlisted-verb-routed", "r12-hyphen-prefix-form",
                  "r12-earlier-item-no-locus"):
            self._group(g, True)

    def test_green_r12_the_closed_head_grammar_still_admits_its_shapes_and_the_oracle_agrees(self):
        self._group("r12-allow-head", False)
        for g, refuse, text in self.rows:
            if g == "r12-allow-head":
                self.assertTrue(_e1_terminal_in_grammar(text), text)
            elif g.startswith("r12-") and refuse:
                self.assertFalse(_e1_terminal_in_grammar(text), text)

    def test_red_r13_an_executable_or_file_argument_never_hides_in_front_of_a_venue_routed_locally(self):
        """r13: every executable x file argument x every clause and sentence boundary is a launch with no route and is refused."""
        self._group("r13-executable-as-object", True)

    def test_green_r13_a_routed_executable_passes_and_the_oracle_agrees(self):
        self._group("r13-allow-routed-executable", False)
        for g, refuse, text in self.rows:
            if g == "r13-allow-routed-executable":
                self.assertTrue(_e1_terminal_in_grammar(text), text)
            elif g.startswith("r13-") and refuse:
                self.assertFalse(_e1_terminal_in_grammar(text), text)

    def test_r13_executables_are_not_object_words(self):
        """r13: an executable is a launch, never a stand-alone object phrase -- it lives in its own list, disjoint from HeadObjectWords."""
        self.assertTrue(self.lists.get("HeadExecutableWords"), "list HeadExecutableWords is missing or empty")
        self.assertEqual([], [w for w in self.lists["HeadExecutableWords"] if w in self.lists["HeadObjectWords"]])
        self.assertEqual([], [w for w in self.lists["HeadObjectWords"] if w.lower().endswith(".exe") or w.lower().startswith("mlvapp")])
        body = LINT_PS1.read_text(encoding="utf-8")
        body = body[body.index("function Get-LocalPlaybackPromptFindings"):]
        self.assertIn("$lists.HeadExecutableWords", body, "the findings function does not read list HeadExecutableWords")

    def test_r12_head_lists_are_exposed_read_by_the_lint_and_closed(self):
        """r12: the head grammar's lists are exposed and read by the findings function; the old blacklist machinery is gone, and no
        verb of the head is anything but a verb the lint itself lists."""
        for key in ("LaunchVerbs", "HeadObjectWords", "HeadFillers", "HeadJoiners", "HeadProhibitions", "HeadContrasts"):
            self.assertTrue(self.lists.get(key), f"list {key} is missing or empty")
        text = LINT_PS1.read_text(encoding="utf-8")
        body = text[text.index("function Get-LocalPlaybackPromptFindings"):]
        for key in ("LaunchVerbs", "HeadObjectWords", "HeadFillers", "HeadJoiners", "HeadProhibitions", "HeadContrasts"):
            self.assertIn(f"$lists.{key}", body, f"the findings function does not read list {key}")
        for gone in ("Get-LeftoverPrepositionIndex", "Test-HeadClauseBenign", "Test-SentenceHeadBenign", "Test-ItemDirty", "$headState.Budget",
                     "$headState.Memo"):
            self.assertNotIn(gone, text, f"{gone} came back: r12 reads the head by a closed whitelist, not by a blacklist")
        # a destination or purpose word is never an admitted token; neither is a weak negation
        for w in ("to", "into", "from", "for", "with", "without", "no"):
            for key in ("HeadObjectWords", "HeadFillers", "HeadJoiners", "HeadProhibitions", "HeadContrasts", "LaunchVerbs"):
                self.assertNotIn(w, self.lists[key], f"{w!r} entered {key}")

    def test_r12_held_out_pools_are_disjoint_from_the_head_lists(self):
        """A held-out verb that the head lists name would be read through the list, not held out."""
        admitted = {w.lower() for key in ("LaunchVerbs", "HeadObjectWords", "HeadFillers", "HeadJoiners", "HeadProhibitions", "HeadContrasts",
                                          "HostNouns", "HostNames", "HostDeterminers")
                    for w in self.lists[key]}
        self.assertEqual([], [w for w in R12_UNLISTED_VERBS if w.lower() in admitted], "R12_UNLISTED_VERBS overlaps a head list")
        for w in R12_UNLISTED_VERBS:
            self.assertEqual([], [p for p in w.lower().split() if p in admitted], f"unlisted verb {w!r} contains an admitted word")
        self.assertEqual(sorted(R12_FILLER_POOL), sorted(self.lists["HeadFillers"]))

    def test_r11_the_head_rule_reads_the_preposition_list_the_lint_exposes(self):
        """A preposition the lint lists is a host nobody named when it opens a head clause: one prefix row per HostPrepositions member."""
        for p in self.lists["HostPrepositions"]:
            text = f"Run playback {p} my rig locally on Bachelor."
            self.assertGreater(_batch_counts(LINT_PS1, [text], self.tmp, f"prep-{p}")[0], 0, text)

    def test_red_r8_held_out_vocabulary_is_refused(self):
        for g in ("r8-held-out-verbless", "r8-held-out-verb-noun", "r8-held-out-trailing"):
            self._group(g, True)

    def test_red_r8_negation_or_condition_tail_does_not_hide_a_launch(self):
        self._group("r8-negation-tail", True)

    def test_red_r8_a_follow_up_sentence_is_read_with_the_venue_sentence(self):
        self._group("r8-cross-sentence", True)

    def test_red_r8_every_list_member_that_could_reopen_the_tail(self):
        for g in ("r8-handling-then-host", "r8-handling-then-launch", "r8-locus-preposition-then-unknown", "r8-negation-then-launch",
                  "r8-pure-locus-then-launch"):
            self._group(g, True)

    def test_green_r8_negation_and_venue_routing_members_still_pass(self):
        # (r8's handling-verb controls became r10-overrefusal-handling rows: refused, ruling 20261015T1650Z)
        for g in ("r8-negation-governs-host-control", "r8-pure-locus-control", "r8-venue-routing-control"):
            self._group(g, False)

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
    # r10 (ruling 20261015T1650Z): every E1 entry is AND-ed with _e1_terminal_in_grammar, the ruling's grammar restated in Python, so
    # no `locally` row can flip refuse -> pass against master unless its venue phrase is terminal under the closed grammar.
    ALLOWED_REFUSE_TO_PASS = (
        ("E1-locally-routed-onto-a-venue",
         lambda text, f: ((f.get("tpl") == "marker-then-venue" and f.get("M") == "locally")
                          or text in ("Run playback locally on Bachelor through the venue chain.", "Run playback locally using Bachelor."))
         and _e1_terminal_in_grammar(text),
         "E1, Sol review of #401: `locally` immediately followed by a routing preposition and a venue word ('Run playback locally on "
         "Bachelor', 'locally through the venue chain') names the venue the work runs on, so it is not a local launch."),
        ("E1-per-member-plain-forms",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") in ("e1-preposition-control", "venue-word-control")
         and _e1_terminal_in_grammar(text),
         "r5, ListMemberDefeaterTests: the plain E1 form of every member of RoutingPrepositionsLocally and VenueWords ('Run playback "
         "locally via Bachelor.', '... on ultra-magnus.') -- the same E1 shape as the entry above, one row per list member, nothing "
         "after the venue name."),
        ("E1-locally-on-a-venue-from-here",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") == "follow-phrase-control" and _e1_terminal_in_grammar(text),
         "r4 design note (CoS), listed in r5: 'Run playback locally on Bachelor from here.' -- `from here` says where a venue job is "
         "STARTED from, not where it runs, so it is a locus-preserving phrase (VenueFollowPhrases). Every defeater of it is refused: "
         "'... from here on this host', '... from here, then run it here' (follow-phrase rows)."),
        ("r10-E1-strictly-terminal-under-the-closed-grammar",
         lambda text, f: _e1_terminal_in_grammar(text),
         "r10, hub ruling HUB-TICK 20261015T1650Z (replaces the r6 and r8 entries): the E1 shape followed ONLY by tail fragments of the "
         "closed, verbless allow-grammar -- (A) a locus preposition + venue ('..., then on Ultra-Magnus.'), (B) a pure locus word "
         "('...; afterwards.'), (C) a verbless host negation ('..., never on this host.'). Master refuses the `locally` marker in all of "
         "them. The predicate is the grammar itself, restated in Python independently of the lint, so a flip it cannot parse fails here."),
        ("E2-marker-negated-by-the-contrast-word-before-it",
         lambda text, f: (f.get("tpl") == "venue-contrast-marker" and f.get("M") == "locally")
         or (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_PASS_R6 and text.startswith("Run playback on Bachelor rather than locally")),
         "E2, r1b/r2 (and the r6 ALLOW row 'Run playback on Bachelor rather than locally; then read the log.', the same E2 shape followed by a "
         "semicolon group that names no local host): 'Run playback on Bachelor rather than / instead of / without / nor / , not locally' -- the marker is the first "
         "thing of the contrast complement, so it is what the prompt rules out. (Only `locally` flips: the host and 'here' markers were "
         "already passed by the baseline's sentence-wide weak-negation / venue exemption.)"),
        ("rule4-r2-route-on-launcher-b-with-a-non-launcher-lead",
         lambda text, f: f.get("tpl") == "seed" and text in RULE4_R2_B_PASS,
         "NA2-RULE4-PER-LAUNCHER-SCOPE-1 r2 (listed in the PR body): 'pwsh -File build.ps1; pwsh -File <script> -Backend cpu on Bachelor.' "
         "(RULE4_R2_B_PASS, 15 rows; the app form moved to RULE4_R2_B_APP_REFUSE in r9) hold ONE launcher, the second, and it carries its own venue route; the first call is "
         "not a playback launcher. Master's greedy gap ran over the ';' and scored the match in the first, unrouted clause, so it "
         "refused them; a launcher's gap now stops at ';' and at the next launcher token, so only the real launcher is scored."),
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
        ("r8-strong-negation-after-the-marker-and-later-launches",
         lambda text, f: f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R8,
         "r8: a strong negation (don't / must not / never ...) that comes AFTER `locally` in the same clause ('Run playback locally on "
         "Bachelor, don't wait, then run it here.', 'Run playback locally, don't use the venue chain.') governs something else, not the "
         "marker; master's sentence-wide prohibition exemption passed these. Also: a later clause that launches anything at all "
         "('..., and replay it.') voids the sentence-terminal E1."),
        ("r10-member-defeaters-behind-a-later-negation",
         lambda text, f: f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("r10-") and f.get("R") is True,
         "r10, the defeater row of a TailNegations member ('Run playback locally on Bachelor, never here, then run it.', '..., never on "
         "this rig.'): master's sentence-wide prohibition exemption lets the `never` AFTER the marker exempt the whole sentence; r8 "
         "made a prohibition govern the marker only when it precedes it, and r10's grammar admits `never` only with a listed host "
         "target and nothing after it."),
        ("r11b-play-open-heads-master-does-not-read",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") == "r11-earlier-clause-known" and text.startswith(("Play ", "Open ")),
         "r11b: 'Play playback on the hub-VM and locally on Bachelor.' / 'Open playback on my rig, then locally on Bachelor.' -- `play` "
         "and `open` are launch verbs this PR added (master's launch-verb list stops at run / launch / start / execute / invoke / drive / "
         "kick off), so master finds no launch in a sentence whose only verb is one of them and passes it. The head rule refuses them "
         "like their `Run` twins, which master refuses too."),
        ("hardening-log-handling-tail",
         lambda text, f: f.get("tpl") == "handling-tail" and f.get("M") in ("on this host", "here"),
         "r1b over-refusal kept on purpose (card HUB-VM-LINT-LOG-COPY-OVERREFUSAL-1): 'copy the log on this host' after a venue run "
         "is refused like its `locally` twin; a handling-verb list would open 'Copy the clip locally and run playback.'"),
        ("rule4-per-launcher-scope",
         lambda text, f: f.get("tpl") == "seed" and (text in RULE4_SECOND_LAUNCHER_REFUSE or text in RULE4_R2_REFUSE or text in RULE4_R4_REFUSE
                                                     or text in RULE4_R6_REFUSE or text in RULE4_R7_REFUSE or text in RULE4_R2_B_APP_REFUSE
                                                     or text in RULE4_R10_SEEDS),
         "r10 (hub ruling on CoS review 6230713095, B6, listed in the PR body): the app call before a `;` that cuts it from its playback mode "
         "('Run <app> <clip>; add <mode>.', RULE4_R10_NAMED) is a launch of its own and needs its own route; master refuses the CoS rows "
         "(its app gap crossed the `;`), so against master they are no flip. The no-argument form ('Run <app>; add <mode>.', RULE4_R10_NOARG) "
         "refuses under the same rule: a tightening, master passes most of it. "
         "r9 (hub ruling on CoS review 6230178238, listed in the PR body): the 15 rows `<app> <clip><sep><routed script> on Bachelor.` "
         "(RULE4_R2_B_APP_REFUSE) are an accepted over-refusal -- the first clause is an app launch with a clip and no route of its own. "
         "NA2-RULE4-PER-LAUNCHER-SCOPE-1, CoS post-merge Blocker on #448 (ask 6216180502): rule 4 scores EACH launcher occurrence, so a "
         "venue locus exempts only the launcher it routes. Master read only the first occurrence and passed '<launcher> -Backend cpu on "
         "Bachelor; then run <launcher> -Backend cpu.' and its relatives (RULE4_SECOND_LAUNCHER_REFUSE). r2 (CoS review of 75cb00e1): "
         "the same for a venue-routed lead that is not a launcher, ` or `, a route that belongs to launcher B, and mixed-style pairs "
         "(RULE4_R2_REFUSE). r4 (sol r3 + Iverson on 4b9c9387): the quoted call `& '<script>' -Param` is a launcher, and a launcher "
         "name inside a path argument no longer ends a launcher's argument gap (RULE4_R4_REFUSE). r6 (CoS review of 469e513d, B1 + B2 + "
         "Improvement 1): an app launch whose clip follows an option, has no extension, or is a quoted path with a space is a launch of its "
         "own (RULE4_R6_REFUSE); master refused the B1 / B2 rows, and passes the clip-launch-as-second-launcher rows of Improvement 1. "
         "r7 (CoS review of 0fb1eb8c, B3 + N1 + N2): ANY first argument after the app that is not the next launcher makes the call a launch "
         "of its own, so `--opt=value`, `/opt`, a bare token and an option chain of any length refuse (RULE4_R7_REFUSE, the generated "
         "argument-grammar sweep); master refused the B3 rows (the first app call reaches a mode flag over the next launcher) and passes the "
         "rows whose clip launch is the SECOND launcher (N1)."),
    )

    # r7 PREVIOUS-HEAD gate (CoS Prevention on a595464f). Against the lint of the PR head this round started from, NOTHING may open:
    # the ceiling for refuse -> pass is EMPTY, so any flip needs an entry here with a reason AND the same reason in the PR body. A
    # round that narrows an exemption or a defeater (the a595464f Rule 4 narrowing handed back 14 refusals r5 had gained) shows up
    # here even when master passes the row, which is exactly what the master differential cannot see.
    ALLOWED_PREVIOUS_HEAD_REFUSE_TO_PASS: tuple = (
        ("r12-allow-head-joiner-and-contrast-without-punctuation",
         lambda text, f: f.get("tpl") == "list-member" and f.get("G") == "r12-allow-head" and f.get("R") is False,
         "r12 (listed in the PR body): the r11b head read a joiner or a contrast word only behind a comma and refused 'Run playback on "
         "Bachelor, so locally on Bachelor.' and 'Run playback on Bachelor never here, then run playback locally on Bachelor.' The closed "
         "head grammar admits every member of HeadJoiners / HeadContrasts as its own piece, so a pure venue-routed launch, a joiner and a "
         "verbless host negation pass; the rows are the r12 ALLOW generator (r12-allow-head), refused by master too (`locally` + a "
         "listed launch verb), and each is terminal under the ruling's grammar."),
        ("rule4-r2-route-on-launcher-b-with-a-non-launcher-lead",
         lambda text, f: f.get("tpl") == "seed" and text in RULE4_R2_B_PASS,
         "NA2-RULE4-PER-LAUNCHER-SCOPE-1 r2 (listed in the PR body): 'pwsh -File build.ps1; pwsh -File <script> -Backend cpu on Bachelor.' "
         "(and the app form) has ONE launcher, the second, and it carries its own venue route; the first call is not a playback launcher. "
         "d2f3cd05 (and the r1 head) let the first call's lazy gap run over the ';' so the merged span sat in the unrouted first clause "
         "and the sentence was refused. A launcher's gap now stops at ';' and at the next launcher token, so only the real launcher is "
         "scored, and it is routed (RULE4_R2_B_PASS; 3 rows open, one per separator; the app form is an app launch of its own since r9). "
         "Master refuses them too (its greedy gap scored the match in the first, unrouted clause), so against master these rows are refuse "
         "-> pass flips, as against d2f3cd05."),
    )
    ALLOWED_PREVIOUS_HEAD_PASS_TO_REFUSE = (
        ("r10-strictly-terminal-e1",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R10 + PromptLintTests.CLAUSE_MUST_REFUSE_R10_OVERREFUSAL)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("r10-") and f.get("R") is True),
         "r10, hub ruling HUB-TICK 20261015T1650Z: E1 is strictly terminal -- the tail after the venue phrase must match a closed, verbless "
         "allow-grammar. The r9 tip passed a launch hidden behind a handling clause and an adverb ('..., then read the log to quietly "
         "replay it.'), a launch lead before a destination preposition ('..., then play the clip from the venue chain.'), and every "
         "harmless handling follow-up ('..., then read the log.'): the first two are the r9 keys' blockers (CLAUSE_MUST_REFUSE_R10, the "
         "r10-* generators), the last is the accepted over-refusal of the ruling (CLAUSE_MUST_REFUSE_R10_OVERREFUSAL, "
         "r10-overrefusal-handling), listed in the PR body. Master refuses every one of them too."),
        ("r11b-e1-head-must-be-benign",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R11)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("r11-") and f.get("R") is True),
         "r11b, opus r10 key on d291f184: E1 reads the HEAD as well as the tail. An earlier clause or item that launches somewhere "
         "(or names a host nobody listed: a preposition left once venue-routed phrases are masked) is not benign, and neither is the "
         "marker clause's own prefix; a weak negation never makes a head benign. The r10 tip passed 'Run playback on the hub-VM and "
         "locally on Bachelor.', '... on my rig, then locally on Bachelor.' and the cross-sentence form (CLAUSE_MUST_REFUSE_R11, the "
         "r11-* generators). The accepted over-refusal is any earlier un-routed launch verb of the block ('Run the build, then run "
         "playback locally on Bachelor.', r11-overrefusal-earlier-launch), listed in the PR body. Master refuses every one of them."),
        ("r12-closed-head-grammar",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R12)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("r12-") and f.get("R") is True),
         "r12, opus + sol r11b keys on c574ea89: the HEAD is read by ONE closed whitelist, not by the lint's own verb / host / "
         "preposition lists. The r11b tip passed an earlier sentence or clause with an unlisted verb ('Re-run the arms on this host. "
         "Then run playback locally on Bachelor.', 'Replay the clip here. ...', 'Preview the clip on this tablet. ...') because its "
         "item veto only analysed an item with a listed launch verb and its word boundary hid `run` in `Re-run` and `play` in "
         "`Replay`; the unlisted-verb rows (CLAUSE_MUST_REFUSE_R12, the r12-* generators) now refuse, and master refuses every one of "
         "them. The accepted over-refusal is an unlisted verb with a VENUE route ('Replay the arms on Bachelor, then run playback "
         "locally on Bachelor.', r12-overrefusal-unlisted-verb-routed) and any earlier clause, item or heading outside the grammar, "
         "listed in the PR body."),
        ("r13-executable-is-a-launch-not-an-object",
         lambda text, f: (f.get("tpl") == "seed" and text in PromptLintTests.CLAUSE_MUST_REFUSE_R13)
         or (f.get("tpl") == "list-member" and str(f.get("G", "")).startswith("r13-") and f.get("R") is True),
         "r13, sol r12 key on d2f3cd05 (HUB-VM-LINT-HEAD-EXECUTABLE-AS-OBJECT-1): an executable, or a clip / file argument, standing "
         "alone is a LAUNCH with no route, never an object phrase. The r12 tip read the app executable as a stand-alone object phrase "
         "and passed 'MLVApp.exe <file>; run playback locally on Bachelor.' and its sentence-break and line-break forms "
         "(CLAUSE_MUST_REFUSE_R13, the r13-* generators); they now refuse, and master refuses every one of them. Sol's repro texts "
         "named a real clip file; the fixtures use neutral made-up file arguments instead (the rule is that ANY file argument after an "
         "executable is a launch). An executable that carries its own route, follows a prohibition, or is the marker clause's own "
         "launch still passes (CLAUSE_MUST_PASS_R13)."),
        ("rule4-per-launcher-scope",
         lambda text, f: f.get("tpl") == "seed" and (text in RULE4_SECOND_LAUNCHER_REFUSE or text in RULE4_R2_REFUSE or text in RULE4_R4_REFUSE
                                                     or text in RULE4_R6_REFUSE or text in RULE4_R7_REFUSE or text in RULE4_R2_B_APP_REFUSE
                                                     or text in RULE4_R10_SEEDS),
         "r10 (hub ruling on CoS review 6230713095, B6, listed in the PR body): the app call before a `;` that cuts it from its playback mode "
         "('Run <app> <clip>; add <mode>.', RULE4_R10_NAMED) is a launch of its own and needs its own route; master refuses the CoS rows "
         "(its app gap crossed the `;`), so against master they are no flip. The no-argument form ('Run <app>; add <mode>.', RULE4_R10_NOARG) "
         "refuses under the same rule: a tightening, master passes most of it. "
         "r9 (hub ruling on CoS review 6230178238, listed in the PR body): the 15 rows `<app> <clip><sep><routed script> on Bachelor.` "
         "(RULE4_R2_B_APP_REFUSE) are an accepted over-refusal -- the first clause is an app launch with a clip and no route of its own. "
         "NA2-RULE4-PER-LAUNCHER-SCOPE-1, CoS post-merge Blocker on #448 (ask 6216180502): a venue-routed first launcher no longer "
         "exempts a second, unrouted one after ';', ', then' or ' then' (RULE4_SECOND_LAUNCHER_REFUSE). The d2f3cd05 lint passed every "
         "one of them (rule 4 read only the first launcher occurrence), and master passes them too. r2 (CoS review of 75cb00e1): a "
         "launcher's gap stops at ';' and at the next pwsh / powershell / app token and spans of different clauses never merge, so "
         "RULE4_R2_REFUSE (cross-clause pairs, ` or `, a route on launcher B only, mixed styles) refuses as well. r4: the quoted call "
         "and a launcher name inside a path argument (RULE4_R4_REFUSE), passed by d2f3cd05 and master alike. r6: an app launch whose clip "
         "follows an option, has no extension, or is a quoted path with a space (RULE4_R6_REFUSE): d2f3cd05 and master "
         "refuse 60 of the 182 RED rows (the six B1 rows and both B2 rows among them) and pass the other 122, which are the rows that flip. "
         "r7: any first argument after the app that is not the next launcher is a launch of its own (RULE4_R7_REFUSE, the generated "
         "argument-grammar sweep): d2f3cd05 and master refuse only the rows whose first app call reaches a mode flag over a later "
         "launcher (196 of the 913 unrouted rows, the B3 rows among them) and pass the rest, which are the rows that flip."),
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hubvm-diff-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_r11_multiline_rows_flip_only_as_listed_against_master_and_the_previous_head(self):
        """r11b / r12: the single-line differential corpus cannot hold a line break, so every multi-line catalogue row is run through
        master, the previous PR head and this lint here. A flip against the previous head needs a reason: NOTHING opens (the hard-wrap
        rows opened against d291f184 in r11b and are the previous head's own verdict now), and the only rows that close are the r12
        multi-line rows (an earlier unlisted launch, an earlier heading outside the closed head grammar)."""
        refuse_rows = (PromptLintTests.CLAUSE_MUST_REFUSE_R8_MULTILINE + PromptLintTests.CLAUSE_MUST_REFUSE_R10_MULTILINE
                       + PromptLintTests.CLAUSE_MUST_REFUSE_R11_MULTILINE + PromptLintTests.CLAUSE_MUST_REFUSE_R12_MULTILINE
                       + PromptLintTests.CLAUSE_MUST_REFUSE_R13_MULTILINE)
        pass_rows = (PromptLintTests.CLAUSE_MUST_PASS_R8_MULTILINE + PromptLintTests.CLAUSE_MUST_PASS_R11_MULTILINE
                     + PromptLintTests.CLAUSE_MUST_PASS_R12_MULTILINE)
        texts = list(refuse_rows + pass_rows)
        base = _baseline_lint_source()
        prev_ref, prev_text = _require_previous_head_lint()
        (self.tmp / "ml-prev.ps1").write_text(prev_text, encoding="utf-8")
        cand = _json_counts(LINT_PS1, texts, self.tmp, "ml-cand")
        prev = _json_counts(self.tmp / "ml-prev.ps1", texts, self.tmp, "ml-prev")
        for text, n in zip(texts, cand):
            self.assertEqual(text in refuse_rows, n > 0, f"candidate verdict of {text!r}")
        opened = [t for t, c, p in zip(texts, cand, prev) if p > 0 and c == 0]
        closed = [t for t, c, p in zip(texts, cand, prev) if p == 0 and c > 0]
        self.assertEqual([], opened, f"against {prev_ref}: no multi-line refuse -> pass flip is allowed in r12")
        self.assertEqual([], [t for t in closed if t not in PromptLintTests.CLAUSE_MUST_REFUSE_R11_MULTILINE + PromptLintTests.CLAUSE_MUST_REFUSE_R12_MULTILINE
                              + PromptLintTests.CLAUSE_MUST_REFUSE_R13_MULTILINE],
                         f"against {prev_ref}: an unlisted multi-line pass -> refuse flip")
        # the previous head (d2f3cd05) is r12's own tip, so the r12 rows are its verdict now; the r13 rows are this round's closures
        self.assertEqual(sorted(PromptLintTests.CLAUSE_MUST_REFUSE_R13_MULTILINE), sorted(t for t in closed if t in PromptLintTests.CLAUSE_MUST_REFUSE_R13_MULTILINE),
                         f"against {prev_ref}: every r13 multi-line refusal row must be a closure (RED at the previous head)")
        if base is not None and base[1].replace("\r\n", "\n") != LINT_PS1.read_text(encoding="utf-8").replace("\r\n", "\n"):
            (self.tmp / "ml-master.ps1").write_text(base[1], encoding="utf-8")
            master = _json_counts(self.tmp / "ml-master.ps1", texts, self.tmp, "ml-master")
            flipped = [t for t, c, m in zip(texts, cand, master) if m > 0 and c == 0]
            allowed = (PromptLintTests.CLAUSE_MUST_PASS_R8_MULTILINE + PromptLintTests.CLAUSE_MUST_PASS_R11_MULTILINE
                       + PromptLintTests.CLAUSE_MUST_PASS_R12_MULTILINE)
            self.assertEqual([], [t for t in flipped if t not in allowed], f"against {base[0]}: an unlisted multi-line refuse -> pass flip")

    def test_no_unlisted_flip_against_the_baseline(self):
        ref, opened, closed, size = _differential_flips(self.tmp)
        self.assertGreaterEqual(size, 1000, "the corpus shrank; the pin no longer samples the wording space")
        problems = _pin_problems(opened, closed, self.ALLOWED_REFUSE_TO_PASS, self.ALLOWED_PASS_TO_REFUSE)
        self.assertEqual(
            [], problems,
            f"baseline {ref}: a safety guard may open or close only on purpose -- fix the lint, or add an allow-list entry with its reason.")
        # Every catalogue row that must be refused is a corpus seed and must not be among the flips at all.
        flipped = {text for text, _ in opened}
        for text in (PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_REFUSE_R10 + PromptLintTests.CLAUSE_MUST_REFUSE_R10_OVERREFUSAL
                     + PromptLintTests.CLAUSE_MUST_REFUSE_R11 + PromptLintTests.CLAUSE_MUST_REFUSE_R12 + PromptLintTests.CLAUSE_MUST_REFUSE_R13):
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
        for text in (PromptLintTests.CLAUSE_MUST_REFUSE + PromptLintTests.CLAUSE_MUST_REFUSE_R7 + PromptLintTests.CLAUSE_MUST_REFUSE_R8
                     + PromptLintTests.CLAUSE_MUST_REFUSE_R10 + PromptLintTests.CLAUSE_MUST_REFUSE_R10_OVERREFUSAL
                     + PromptLintTests.CLAUSE_MUST_REFUSE_R11 + PromptLintTests.CLAUSE_MUST_REFUSE_R12 + PromptLintTests.CLAUSE_MUST_REFUSE_R13):
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
