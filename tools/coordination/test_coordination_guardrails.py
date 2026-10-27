import glob
import hashlib
import os
import re
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "tools" / "coordination" / "validate_and_append_handoff.py"
WATCHDOG = ROOT / "tools" / "coordination" / "coordination_watchdog.py"
HEARTBEAT = ROOT / "tools" / "coordination" / "coordination-heartbeat.ps1"


def run(*args, cwd):
    return subprocess.run([sys.executable, *map(str, args)], cwd=cwd, text=True, capture_output=True)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def init_repo(tmp_path):
    r"""`git init` a throwaway repo that survives a long TMPDIR.

    MEASURED 2026-09-05. With TMPDIR under a deep path, eleven tests in this file failed with
    `git add` reporting `Filename too long`. pytest's tmp_path adds
    `pytest-of-<user>/pytest-N/<testname>0/` on top of TMPDIR -- 220 chars here -- and Windows
    MAX_PATH bites at 260. The suite passed from `C:\mlvtmp\pt` and failed from the agent
    scratchpad, on the same commit and interpreter, so the result depended on WHERE it ran.
    A test that passes or fails by its temp directory is not testing what it claims to.

    THE ORDER MATTERS AND WAS FALSIFIED, because the obvious fix does not work:
      `git init` then `git config core.longpaths true`  -> init ITSELF dies, stat'ing
          .git/hooks/fsmonitor-watchman.sample, before any config can be set.
      `git -c core.longpaths=true init` alone           -> init succeeds, but -c is transient,
          so the very next bare `git add` fails exactly as before.
    Only -c ON THE INIT, followed by persisting it into the new repo, makes every later bare
    git call in these helpers safe.

    The repo already reaches for this mitigation elsewhere -- brokered_closeout.py's
    run_git_longpaths() and Invoke-WorkstreamLoop.ps1's worktree add both pass
    `-c core.longpaths=true`. These helpers were simply never given it.

    CI never sees this: hosted runners put RUNNER_TEMP at a short path like D:\_temp, which
    is why the trap survived to bite local runs only.
    """
    subprocess.run(
        ["git", "-c", "core.longpaths=true", "init", "-q"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "core.longpaths", "true"], cwd=tmp_path, check=True
    )


# COST (CI-SHARD8-GUARDRAILS-COST-1): the git repositories the fixtures below build cost six to forty-five git
# processes each and were rebuilt for every test that uses one (editing_board alone is called by 27 tests,
# coverage_repo by 17). Each distinct repository is now built ONCE per pytest session by the original builder
# (the `_build_*` functions, bodies unchanged) in a directory of its own, and every test copies that directory
# tree into its own tmp_path, so no test can see another's writes. Only repositories whose `.git` holds no
# absolute path are templated this way (no remote); stale_ref_repo, whose clone remembers its origin's path, is not.
_FIXTURE_TEMPLATES = {}
_TEMPLATE_BASE = {}


@pytest.fixture(scope="session", autouse=True)
def _fixture_template_base(tmp_path_factory):
    _TEMPLATE_BASE["dir"] = tmp_path_factory.getbasetemp()


def fixture_template(key, build):
    """Run `build(root)` once per `key` in a fresh directory of the session's base temp dir; return (root, its result)."""
    if key not in _FIXTURE_TEMPLATES:
        root = Path(tempfile.mkdtemp(prefix="guardrails-fixture-template-", dir=_TEMPLATE_BASE["dir"]))
        _FIXTURE_TEMPLATES[key] = (root, build(root))
    return _FIXTURE_TEMPLATES[key]


def make_repo(tmp_path):
    root, shas = fixture_template(("make_repo",), _build_make_repo)
    shutil.copytree(root, tmp_path, dirs_exist_ok=True)
    return shas


def _build_make_repo(tmp_path):
    init_repo(tmp_path)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    start = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "seed.txt").write_text("feature\n")
    subprocess.run(["git", "commit", "-qam", "feature"], cwd=tmp_path, check=True)
    feature = git(tmp_path, "rev-parse", "HEAD")
    return start, feature


def args(repo, start, feature, *extra):
    coord = repo / ".claude-state" / "coordination"
    coord.mkdir(parents=True)
    ledgers = [coord / "gpu.md", coord / "claude.md", coord / "codex.md"]
    for ledger in ledgers:
        ledger.write_text("# ledger\n")
    return [
        "--repo-root", repo, "--start", start, "--feature", feature,
        "--work-block", "wb-test", "--summary", "test handoff",
        "--changes", "coordination only", "--validation", "focused test pass",
        "--proof-boundary", "no product claim", "--request", "review exact range",
        "--ledger", ledgers[0], "--ledger", ledgers[1], "--codex-ledger", ledgers[2], *extra,
    ], ledgers


def test_validator_rejects_phantom_feature(tmp_path):
    start, feature = make_repo(tmp_path)
    command, _ = args(tmp_path, start, "e7311126f0f638f01eb06808f99a5e62908fc7b8")
    result = run(VALIDATOR, *command, cwd=tmp_path)
    assert result.returncode == 2
    assert "HANDOFF_BLOCKED" in result.stderr


def test_validator_mirrors_exact_payload(tmp_path):
    start, feature = make_repo(tmp_path)
    command, ledgers = args(tmp_path, start, feature)
    result = run(VALIDATOR, *command, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    range_token = f"{start}..{feature}"
    for ledger in ledgers:
        text = ledger.read_text()
        assert text.endswith("Reviewer: Claude\n")
        assert range_token in text
        assert "WorkBlock: wb-test" in text


def test_watchdog_escalates_after_two_missed_reviews(tmp_path):
    start, feature = make_repo(tmp_path)
    command, ledgers = args(tmp_path, start, feature)
    assert run(VALIDATOR, *command, cwd=tmp_path).returncode == 0
    state_file = tmp_path / ".claude-state" / "coordination" / "state.json"
    watch = ["--repo-root", tmp_path, "--state-file", state_file, "--missed-heartbeats", "2", "--ledger", ledgers[0], "--ledger", ledgers[1], "--ledger", ledgers[2]]
    result = run(WATCHDOG, *watch, cwd=tmp_path)
    assert result.returncode == 3
    assert json.loads(result.stdout)["state"] == "STALL"


def test_watchdog_repairs_missing_mirror_but_requires_ack(tmp_path):
    start, feature = make_repo(tmp_path)
    command, ledgers = args(tmp_path, start, feature)
    assert run(VALIDATOR, *command, cwd=tmp_path).returncode == 0
    codex_ledger = ledgers[2]
    codex_ledger.write_text("# ledger\n")
    state_file = tmp_path / ".claude-state" / "coordination" / "state.json"
    watch = ["--repo-root", tmp_path, "--state-file", state_file, "--repair-mirrors", "--ledger", ledgers[0], "--ledger", ledgers[1], "--ledger", ledgers[2]]
    result = run(WATCHDOG, *watch, cwd=tmp_path)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["state"] == "ACK_REQUIRED"
    assert payload["repairedMirrors"] == [str(codex_ledger)]


def test_heartbeat_resolves_watchdog_beside_wrapper_not_under_repo_root(tmp_path):
    coord = tmp_path / ".claude-state" / "coordination"
    dual = coord / "dual-lane"
    dual.mkdir(parents=True)
    (coord / "gpu-lane-impl-review-sync.md").write_text("# gate\n")
    (dual / "claude.md").write_text("# claude\n")
    (dual / "codex.md").write_text("# codex\n")
    result = subprocess.run(
        [
            "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(HEARTBEAT),
            "-RepoRoot", str(tmp_path), "-Once",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "IDLE"
    assert not (tmp_path / "tools" / "coordination" / "coordination_watchdog.py").exists()


def test_supplied_timestamp_must_be_a_real_instant(tmp_path):
    """STAMP-APPENDER-1: --timestamp was written into the entry verbatim, so NOT-A-CLOCK
    reached the ledger heading. The closeout gate BLOCKS on a heading it cannot parse
    (content_approval_unparsable_heading), so this appender could stop finalize by itself.
    A real instant must still pass through UNCHANGED: --timestamp exists for replay and
    fixtures, and silently rewriting it would surprise those callers."""
    start, feature = make_repo(tmp_path)

    # subject: a non-instant is refused before anything is appended
    command, ledgers = args(tmp_path, start, feature)
    result = run(VALIDATOR, *command, "--timestamp", "NOT-A-CLOCK", cwd=tmp_path)
    assert result.returncode != 0, result.stdout
    assert "ISO-8601" in (result.stderr + result.stdout)
    for ledger in ledgers:
        assert "NOT-A-CLOCK" not in ledger.read_text()

    # control: a real instant is accepted AND preserved byte-for-byte
    second = tmp_path / "second"
    second.mkdir()
    start2, feature2 = make_repo(second)
    command2, ledgers2 = args(second, start2, feature2)
    exact = "2000-01-02T03:04:05+00:00"
    ok = run(VALIDATOR, *command2, "--timestamp", exact, cwd=second)
    assert ok.returncode == 0, ok.stderr
    assert any(exact in ledger.read_text() for ledger in ledgers2)


LOOP = ROOT / "tools" / "coordination" / "Invoke-WorkstreamLoop.ps1"
WORKSTREAM = ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1"
# The landing probe was extracted out of Invoke-Workstream.ps1 so queue-derive.ps1 could consult
# the SAME rules instead of growing a second copy that would be widened once and left stale. The
# three probe tests below follow it: their subject never changed, only which file owns it.
PROBE = ROOT / "tools" / "coordination" / "landing-probe.ps1"
DERIVE = ROOT / "tools" / "coordination" / "queue-derive.ps1"


def test_cycle_receipt_stamp_has_exactly_one_assignment():
    """The cycle-receipt filename is built from $stamp, which must stay a formatted STRING.

    On 2026-09-04 the daily-budget fix (PR #37) reused `$stamp` as a per-row [datetime]
    inside the counting loop -- same script scope, between the assignment and the use. The
    receipt path then interpolated with the current culture as
    'cycle-09\04\2026 09:05:02.json': "/" became directory separators and ":" is illegal
    in a Windows filename, so WriteAllText threw and the scheduled task exited 1 for ~4.5 h
    while writing no audit record.

    Asserted on the source rather than by running the loop: the loop syncs a git worktree,
    reads the live dispatch log and writes into .claude-state, so executing it under test
    would mutate real board state. The repo already asserts source shape this way in
    tools/repo_hygiene/test_repo_hygiene.py.
    """
    text = LOOP.read_text(encoding="utf-8")

    # the receipt is still built from $stamp
    assert 'cycle-$stamp.json' in text

    # ...and $stamp is assigned exactly once, at script scope
    assignments = re.findall(r"^\s*\$stamp\s*=", text, re.MULTILINE)
    assert len(assignments) == 1, (
        "$stamp must have exactly one assignment; a second one clobbers the receipt "
        "filename. Found %d." % len(assignments)
    )

    # ...and that one assignment produces a filename-safe format, not a culture default
    assert re.search(r"\$stamp\s*=\s*\$cycleStart\.ToString\('yyyyMMddTHHmmssZ'\)", text), (
        "$stamp must be formatted with an explicit invariant pattern; a culture-default "
        "ToString() yields '/' and ':' which are illegal in a Windows path."
    )


def test_a_transient_fetch_failure_does_not_halt_the_cycle():
    """One blip must not cost a whole 45-minute cycle.

    Observed 2026-09-04T18:01:22Z: the loop wrote
    haltedReason="git fetch fork failed (exit 128); driver worktree may be stale" and
    dispatched nothing. The cause was lock contention with a concurrent git operation on the
    same repository -- the identical fetch succeeded by hand moments later, and the driver
    worktree was neither stale nor broken. The old code halted on the FIRST non-zero exit, so
    a blip and a genuinely broken remote were indistinguishable.

    Asserted on the source for the reason documented in
    test_cycle_receipt_stamp_has_exactly_one_assignment: running the loop syncs a real git
    worktree and writes into .claude-state, so it would mutate live board state.
    """
    text = LOOP.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert "function Invoke-GitFetchFork" in text, "no bounded retry helper"

    # the helper must attempt more than once
    attempts_line = [ln for ln in lines if "$Attempts" in ln and "param(" in ln]
    assert attempts_line, "Invoke-GitFetchFork must take a bounded $Attempts parameter"
    digits = "".join(ch for ch in attempts_line[0].split("$Attempts", 1)[1] if ch.isdigit())
    assert digits and int(digits[0]) > 1, "a single attempt is not a retry"

    # THE ANTI-PATTERN: a bare fetch whose very next line halts the cycle
    stale = "driver worktree may be stale"
    for i, ln in enumerate(lines):
        is_bare_fetch = ("git -C $RepoRoot fetch fork" in ln) and ("Invoke-GitFetchFork" not in ln)
        if not is_bare_fetch:
            continue
        following = lines[i + 1] if i + 1 < len(lines) else ""
        assert stale not in following, (
            "line %d halts the cycle on a single fetch attempt: %s" % (i + 1, ln.strip())
        )

    # the halt message reports the attempt count, so receipts stay diagnosable
    assert "attempts; " + stale in text


def test_landing_probe_knows_the_verbs_and_connectors_that_actually_get_used():
    """Two MEASURED misses on 2026-09-05, both from real merged PR bodies.

    PR #53: "Closes OWN-2 and delivers GATE-RESIDUALS-1(b)" -- OWN-2 was skipped,
    GATE-RESIDUALS-1 was not, and the loop spent a lane on it at 02:11Z.
    PR #52: "Closes queue item OWN-1-PRECEDENCE" -- only "card" was permitted between the verb
    and the id, so that missed too, and the near-miss diagnostic surfaced it on its first run.

    The vocabulary is a fixed list, and every writer who does not know it costs a dispatch.
    """
    text = PROBE.read_text(encoding="utf-8")
    assert "delivers" in text, "landing verb 'delivers' not accepted"
    assert "queue" in text, "'queue item' connector not accepted"


def test_a_bare_id_in_prose_still_does_NOT_count_as_landing():
    """The load-bearing asymmetry, from the probe's own comment: a false positive SKIPS
    genuinely open work, which is strictly worse than re-dispatching finished work. Widening
    the vocabulary must never reach a bare mention."""
    text = PROBE.read_text(encoding="utf-8")
    line = [l for l in text.splitlines() if "$script:LandingVerbs" in l and "=" in l]
    assert line, "landing verb list not found"
    assert "lands|closes|fixes|resolves|delivers" in line[0], (
        "the match is no longer gated behind an explicit landing verb"
    )


def test_near_misses_are_summarised_not_printed_per_card():
    """The first draft printed one line per near-miss and produced TEN on a single run, mostly
    genuine prose mentions. A diagnostic that fires every run is one nobody reads -- which is
    exactly the failure it exists to prevent."""
    text = WORKSTREAM.read_text(encoding="utf-8")
    assert "$nearMisses" in text, "near-miss diagnostic absent"
    assert "NEAR-MISS ($($nearMisses.Count))" in text, "near-misses are not summarised into one line"
    assert "NOT skipped" in text, "the diagnostic must say it did not act on the near-miss"


# --- pre-dispatch hosted GitHub evidence ----------------------------------------------------
# MEASURED 2026-09-05: FACTORY-MATURITY-1-CLAUDE and FACTORY-MATURITY-1-OPUS, both priority 1,
# consumed two lane slots each and both returned the same wall -- `gh` answers "Access is denied"
# inside the read-only lane sandbox. The same gh, token and machine work from an interactive
# session; gh resolves its token through the Windows credential keyring and the sandbox denies
# that read. PR #55 had already TOLD lanes this might happen and that saying so is a FINDING, and
# both lanes did say so, correctly -- and were dispatched into the wall anyway, because the brief
# only taught them to report the limitation and never removed it.
#
# Asserted on the source, for the reason given in
# test_cycle_receipt_stamp_has_exactly_one_assignment: running Invoke-Workstream reads the live
# queue and writes a prompt and a run directory into real .claude-state, so executing it under
# test would mutate board state. Behavioural verification is a subject/falsifier pair run by hand
# (real gh -> "gh-evidence 5/5 export(s) ok"; a stub gh on PATH that exits 1 with "Access is
# denied" -> "gh-evidence 0/4", the section headed "THE EXPORT FAILED", and dispatch continues).


def test_hosted_evidence_is_collected_by_the_dispatcher_not_left_to_the_lane():
    """A warning in a prompt is not a fix; it is a nicer way to fail. The dispatcher runs in the
    venue where gh works and was already calling it for the landing probe, so the evidence was
    always one command away from the process doing the dispatching."""
    text = WORKSTREAM.read_text(encoding="utf-8")
    assert "$needsHostedEvidence" in text, "no hosted-evidence classification"
    assert "github-evidence" in text, "no export directory beside the run"
    # the export must actually shell out to gh from here, not merely describe it
    assert "& gh @ghArgs" in text, "the dispatcher does not itself invoke gh for the export"
    # ...and the brief must forbid the lane from retrying it. Doubled backticks: inside the
    # here-string a backtick is PowerShell's escape character, so ``gh`` is what renders as `gh`.
    assert "DO NOT RETRY ``gh`` YOURSELF" in text, "the brief does not stop the lane re-hitting the wall"


def test_the_run_directory_is_named_before_the_brief_that_cites_it():
    """The brief has to name files this script is about to write. $runDir was originally computed
    at dispatch time, AFTER the brief and only on the non-dry-run path -- so a section naming the
    export directory could not exist, and -DryRun could not be used to inspect one.

    Exactly one assignment, for the same reason $stamp has exactly one: a second one further down
    would silently point the lane at a directory that never receives the export.
    """
    text = WORKSTREAM.read_text(encoding="utf-8")
    for var in ("$runDir", "$stamp"):
        assignments = re.findall(r"(?m)^\s*" + re.escape(var) + r"\s*=", text)
        assert len(assignments) == 1, (
            "%s must have exactly one assignment; found %d" % (var, len(assignments))
        )
    assert text.index("$runDir = Join-Path") < text.index("$brief = @\""), (
        "$runDir is assigned after the brief is built, so the brief cannot name the export"
    )


def test_the_export_fails_open_and_says_why_rather_than_only_that_it_failed():
    """Fail-open, exactly like the landing probe: an unreadable signal must never silently shrink
    the board. And the REASON is kept, not just the exit code -- 'Access is denied' and a 404 send
    a reader to completely different places, and collapsing both to 'gh failed' is how a venue
    problem gets misfiled as an authorization problem for a second time."""
    text = WORKSTREAM.read_text(encoding="utf-8")
    assert "CANNOT-DETERMINE: gh not on PATH" in text, "a missing gh must not be fatal"
    assert "$why" in text and "no stderr" in text, "the failure reason is not preserved"

    # nothing between the export block and the READ-ONLY dispatch's own -DryRun check may exit:
    # a failed export must still dispatch, with the lane told plainly that those facts are
    # UNVERIFIED. Scoped to AFTER "the brief" marker (rather than the first "if ($DryRun)" in the
    # file) because TOOL-LOOP-PLUMBING-1 added a second, earlier dispatch path (-AllowEdits) with
    # its own unrelated -DryRun check.
    start = text.index("$needsHostedEvidence =")
    brief = text.index("# ------------------------------------------------------------------ the brief")
    end = text.index("if ($DryRun)", brief)
    assert not re.search(r"(?m)^\s*exit\s+\d", text[start:end]), (
        "the hosted-evidence export can halt dispatch; it must fail open"
    )
    assert "UNVERIFIED" in text[start:end], "a failed export must tell the lane the facts are UNVERIFIED"


def test_dry_run_admits_the_export_already_happened():
    """-DryRun really does write the export -- that is the point, it is how you inspect what a
    lane would receive. Printing a bare 'nothing dispatched' would be a lie by omission about a
    directory this command just created. TOOL-LOOP-PLUMBING-1 added a second dry-run notice for
    the -AllowEdits path (about its own real-then-removed worktree) -- checking ANY line here,
    not just the first, keeps this test about the read-only path's own disclosure."""
    text = WORKSTREAM.read_text(encoding="utf-8")
    dry = [ln for ln in text.splitlines() if "DRY RUN" in ln and "Write-Output" in ln]
    assert dry, "no dry-run notice"
    assert any("on disk" in ln for ln in dry), "dry run does not disclose that the export is real"



def test_the_title_match_is_verb_gated_exactly_like_the_body_match():
    """MEASURED 2026-09-05. The title match used to be unconditional, on the theory that a card id
    in a merge subject implies a landing. PR #41 falsifies it: titled "(addresses
    STAMP-APPENDER-1)", body saying "this PR says addresses, not lands: a false landing signal
    would mark the card done and skip remaining work". The probe skipped the open card anyway.

    Gating costs nothing: across all 51 merged PRs and 117 queue ids there are exactly three title
    matches, and both real landings (#23, #38) read "lands".
    """
    text = PROBE.read_text(encoding="utf-8")
    body = text[text.index("function Get-CardLandingEvidence"):]
    # Only the LANDING decision is under test. The near-miss detector below it legitimately
    # matches a bare mention against the title -- that is its whole job, and it reports rather
    # than acting. The line that assigns $hit is the one that decides "landed".
    decisions = [l for l in body.splitlines() if "$hit = " in l and "-match" in l]
    assert len(decisions) == 2, "expected exactly a title and a body landing decision, got %d" % len(decisions)
    for line in decisions:
        assert "$rxLanding" in line and "$rxMention" not in line, (
            "a landing decision is made on something other than the verb-gated regex: %s" % line.strip()
        )
    assert any("$_.title" in l for l in decisions), "the title is no longer consulted at all"


def test_the_near_miss_matcher_has_a_word_boundary_on_BOTH_sides():
    """MEASURED 2026-09-05. The landing regex always had a leading `(?<![A-Za-z0-9-])`; the
    near-miss regex did not, and nobody noticed because it only ever over-reports. PR #42's body
    names the PROMPT FILE `ws-GATE-ID-3`, and the unanchored matcher found the card id inside that
    filename -- a near-miss for a card that PR never mentioned. Every generated prompt on this
    board is named `ws-<CARD-ID>-<stamp>.md`, so this misfires structurally, not by bad luck."""
    text = PROBE.read_text(encoding="utf-8")
    fn = text[text.index("function Get-MentionRegex"):text.index("function Get-CardLandingEvidence")]
    assert "(?<![A-Za-z0-9-])" in fn, "the mention regex can match inside a longer token (e.g. ws-<ID>)"
    assert "(?![A-Za-z0-9-])" in fn, "the mention regex can match a longer id (C2-SUBMIT-2 vs -22)"


def test_the_probe_is_shared_and_not_copied_into_its_callers():
    """Two tools ask 'did this card land'. A second copy of the vocabulary would be widened once
    and left stale -- this board's most frequently paid failure. Both must DOT-SOURCE the probe,
    and neither may carry its own matcher."""
    for caller in (WORKSTREAM, DERIVE):
        text = caller.read_text(encoding="utf-8")
        assert "landing-probe.ps1" in text, "%s does not consult the shared probe" % caller.name
        assert "Get-CardLandingEvidence" in text, "%s does not call the shared probe" % caller.name
        assert "gh pr list" not in text, (
            "%s re-implements the probe instead of consulting it" % caller.name
        )


def test_the_shared_probe_sets_no_strictmode_on_its_callers():
    """It is DOT-SOURCED, so anything it sets lands in the CALLER's scope. An earlier draft set
    `-Version Latest` and immediately broke queue-derive.ps1, which reads optional queue fields
    that are legitimately absent -- a shared helper silently changing an unrelated script's
    semantics just by being consulted."""
    text = PROBE.read_text(encoding="utf-8")
    active = [l for l in text.splitlines() if "Set-StrictMode" in l and not l.strip().startswith("#")]
    assert not active, "the dot-sourced probe imposes strictness on its callers: %s" % active


def test_queue_derive_fails_open_when_the_landing_probe_cannot_run():
    """An unreadable signal must never silently shrink the board. The probe's status is printed on
    EVERY run, including when it could not run: a check whose failure looks identical to a pass is
    the defect queue-derive.ps1 exists to refuse."""
    text = DERIVE.read_text(encoding="utf-8")
    assert "queue-derive: landing-probe {0}" in text, "probe status is not reported unconditionally"
    assert "NoLandingProbe" in text, "no way to run the ancestry axis without the network"
    probe = PROBE.read_text(encoding="utf-8")
    assert probe.count("cannot-determine:") >= 3, "the probe does not fail open on every failure path"


def test_an_open_card_naming_no_sha_is_reported_not_silently_passed():
    """THE BLIND SPOT THIS EXISTS TO CLOSE. On 2026-09-05 six landed cards named no sha and the
    tool printed NO MISMATCHES. An item the sha axis cannot judge must be VISIBLE, and if a merged
    PR claims it behind a landing verb it must be a MISMATCH -- not folded into either."""
    text = DERIVE.read_text(encoding="utf-8")
    assert "NO-SHA-EVIDENCE" in text, "items the sha axis cannot judge are not tracked"
    assert "STALE-LANDED-IN-PR" in text, "the PR axis produces no finding"
    assert "NOT a mismatch, nothing to check them against" in text, (
        "an unjudgeable item must not be reported as a disagreement"
    )

# --- assert-script-currency.ps1 -------------------------------------------------------------
# The board root is the only checkout carrying .claude-state/, and it routinely sits on a peer
# branch. Running a tool from there by absolute path silently executes a stale copy: the script
# exits 0 and prints a wrong answer. These tests pin the guard that refuses that, and pin the
# fail-OPEN direction -- a guard that blocked on "I could not check" would be worse than the bug.

GUARD = ROOT / "tools" / "coordination" / "assert-script-currency.ps1"
GUARDED = [
    ROOT / "tools" / "coordination" / "queue-derive.ps1",
    ROOT / "tools" / "coordination" / "board-health-sweep.ps1",
]


def guard_status(script_path, ref="fork/master", env=None):
    """Run the guard with -PassThru and return its status, or 'THREW' when it refuses."""
    cmd = (
        "try { (& '%s' -ScriptPath '%s' -Ref '%s' -PassThru).status } "
        "catch { 'THREW' }" % (GUARD.as_posix(), Path(script_path).as_posix(), ref)
    )
    import os
    e = dict(os.environ)
    e.pop("MLV_ALLOW_STALE_TOOLS", None)
    if env:
        e.update(env)
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", cmd], text=True, capture_output=True, env=e
    ).stdout.strip().splitlines()
    return out[-1].strip() if out else ""


def currency_repo(tmp_path):
    root, _ = fixture_template(("currency_repo",), _build_currency_repo)
    shutil.copytree(root, tmp_path, dirs_exist_ok=True)
    return tmp_path / "tool.ps1"


def _build_currency_repo(tmp_path):
    """A repo with a 'fork/master' ref and a tracked script, so the guard has something to compare."""
    init_repo(tmp_path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    script = tmp_path / "tool.ps1"
    script.write_text("Write-Output 'original'\n")
    subprocess.run(["git", "add", "tool.ps1"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    # A local ref literally named refs/remotes/fork/master, so no network or remote is needed.
    subprocess.run(
        ["git", "update-ref", "refs/remotes/fork/master", git(tmp_path, "rev-parse", "HEAD")],
        cwd=tmp_path, check=True,
    )
    return script


def test_currency_guard_passes_when_the_file_matches_the_reference(tmp_path):
    script = currency_repo(tmp_path)
    assert guard_status(script) == "current"


def test_currency_guard_refuses_when_the_file_differs_from_the_reference(tmp_path):
    script = currency_repo(tmp_path)
    script.write_text("Write-Output 'a stale peer-branch copy'\n")
    assert guard_status(script) == "THREW"


def test_currency_guard_refusal_names_the_branch_the_checkout_is_on(tmp_path):
    # The message has to say WHY, or the reader treats it as noise and sets the hatch reflexively.
    script = currency_repo(tmp_path)
    subprocess.run(["git", "checkout", "-qb", "diag/some-peer-branch"], cwd=tmp_path, check=True)
    script.write_text("Write-Output 'drifted'\n")
    cmd = "try { & '%s' -ScriptPath '%s' } catch { $_.Exception.Message }" % (
        GUARD.as_posix(), script.as_posix(),
    )
    import os
    e = dict(os.environ)
    e.pop("MLV_ALLOW_STALE_TOOLS", None)  # an inherited hatch would make this pass vacuously
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", cmd], text=True, capture_output=True, env=e
    )
    blob = out.stdout + out.stderr
    assert "diag/some-peer-branch" in blob
    assert "MLV_ALLOW_STALE_TOOLS" in blob


def test_currency_guard_honours_the_escape_hatch(tmp_path):
    script = currency_repo(tmp_path)
    script.write_text("Write-Output 'deliberately modified'\n")
    assert guard_status(script, env={"MLV_ALLOW_STALE_TOOLS": "1"}) == "skipped"


def test_currency_guard_allows_a_file_not_yet_present_on_the_reference(tmp_path):
    # A brand-new script is not stale. Blocking here would make it impossible to add one.
    script = currency_repo(tmp_path)
    fresh = script.parent / "brand-new.ps1"
    fresh.write_text("Write-Output 'new'\n")
    assert guard_status(fresh) == "untracked-on-ref"


def test_currency_guard_fails_open_when_the_reference_does_not_resolve(tmp_path):
    # A lane sandbox or a fresh clone has no 'fork' remote. That is missing infrastructure,
    # not proven drift, so the guard must stay silent rather than halt the caller.
    script = currency_repo(tmp_path)
    script.write_text("Write-Output 'differs'\n")
    assert guard_status(script, ref="nonexistent/ref") == "unknown"


def test_currency_guard_fails_open_outside_a_git_working_tree(tmp_path):
    loose = tmp_path / "loose.ps1"
    loose.write_text("Write-Output 'x'\n")
    assert guard_status(loose) == "unknown"


def test_the_read_only_board_diagnostics_actually_invoke_the_guard(tmp_path):
    # Without this, the guard could quietly stop being wired and nothing would notice.
    for script in GUARDED:
        body = script.read_text(encoding="utf-8")
        assert "assert-script-currency.ps1" in body, f"{script.name} no longer invokes the guard"
        assert "-ScriptPath $PSCommandPath" in body, f"{script.name} guards the wrong path"

# --- Invoke-Workstream: a malformed priority must not stop dispatch -------------------------
# Get-Rank used a bare [int] cast. On a non-numeric string that THROWS, and the throw lands
# inside Sort-Object -Property { Get-Rank $_ }, killing selection -- while the script still
# exits 0, so the loop records a normal cycle that dispatched nothing. Silent starvation is
# worse than a halt because nothing reports it. queue.json demonstrably carries prose in this
# field (DISPATCH-CDX-14, DISPATCH-CDX-15), so the authoring habit is real, not hypothetical.



def workstream_dry_run(tmp_path, items, track="factory"):
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"schema": "test", "items": items}), encoding="ascii")
    return subprocess.run(
        [
            "pwsh", "-NoProfile", "-File", str(WORKSTREAM),
            "-Track", track, "-DryRun", "-NoLandingProbe", "-QueuePath", str(queue),
        ],
        text=True, capture_output=True,
    )


# 'queued', not 'booked': PR #63 made the dispatcher's admission an ALLOWLIST derived from
# queue.json's own note, where booked means "named trigger, not schedulable". These fixtures
# exist to reach Get-Rank, so they must carry a state a lane actually owns. The rule is right;
# the fixture was relying on booked being dispatchable, which it never should have been.
NUMERIC_CARD = {"id": "NUMERIC-1", "state": "queued", "track": "factory", "priority": 1, "title": "normal"}
PROSE_CARD = {
    "id": "PROSE-1", "state": "queued", "track": "factory",
    "priority": "CRITICAL PATH - the cap is now two blocks away, not one",
    "title": "prose in the priority field",
}


def test_a_prose_priority_does_not_stop_the_dispatcher_selecting(tmp_path):
    out = workstream_dry_run(tmp_path, [NUMERIC_CARD, PROSE_CARD])
    assert "card=NUMERIC-1" in out.stdout, out.stdout + out.stderr
    assert "Sort-Object" not in out.stderr, "selection still dies on the cast"


def test_a_prose_priority_is_reported_loudly_rather_than_swallowed(tmp_path):
    # Ranking it 999 silently would hide a card that someone deliberately marked urgent.
    out = workstream_dry_run(tmp_path, [NUMERIC_CARD, PROSE_CARD])
    assert "NON-NUMERIC PRIORITY" in out.stderr
    assert "PROSE-1" in out.stderr


def test_the_priority_warning_stays_off_machine_readable_stdout(tmp_path):
    # The [WORKSTREAM] stdout lines are parsed, and emitting inside a Sort-Object property
    # block would make the sort key an array rather than an int.
    out = workstream_dry_run(tmp_path, [NUMERIC_CARD, PROSE_CARD])
    assert "NON-NUMERIC PRIORITY" not in out.stdout


def test_the_priority_warning_is_emitted_once_per_card(tmp_path):
    # Sort-Object may evaluate the property block more than once per item.
    out = workstream_dry_run(tmp_path, [NUMERIC_CARD, PROSE_CARD])
    assert out.stderr.count("NON-NUMERIC PRIORITY on card 'PROSE-1'") == 1


def test_a_card_with_a_prose_priority_is_still_dispatchable(tmp_path):
    # Ranked last, not excluded -- a malformed field must not make work unreachable.
    out = workstream_dry_run(tmp_path, [PROSE_CARD])
    assert "card=PROSE-1" in out.stdout, out.stdout + out.stderr


def test_a_wholly_numeric_queue_produces_no_priority_warning(tmp_path):
    out = workstream_dry_run(tmp_path, [NUMERIC_CARD])
    assert "NON-NUMERIC PRIORITY" not in out.stderr
    assert "card=NUMERIC-1" in out.stdout


# --- Invoke-Lane: the exit code must be the truth ------------------------------------------
# The script's own header says "it is alive because it is running and dead when it exits, and the
# exit code is the truth" -- and it did not honour that at its own boundary. It computed the
# lane's exit code, wrote it into the receipt, printed it, then fell off the end, which exits 0.
# Invoke-Workstream captured that 0 and logged laneExitCode=0, so a failed lane was recorded as a
# success. Measured 2026-09-05: two of twelve dispatches hit HTTP 429 ("You've hit your session
# limit"), produced nothing, carried exitCode 1 in their receipts and laneExitCode 0 in the log.

LANE_RUNNER = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"


def test_invoke_lane_propagates_its_exit_code_instead_of_falling_off_the_end():
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "exit $propagated" in body, (
        "Invoke-Lane must exit with the lane's outcome; falling off the end exits 0 and makes "
        "every failed lane look successful to Invoke-Workstream's laneExitCode"
    )


def test_invoke_lane_maps_the_negative_sentinels_rather_than_passing_them_through():
    # -1 would surface as 255 through a process exit code, and -999 does not survive at all.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "-1      { 124 }" in body, "timeout sentinel is not mapped"
    assert "-999    { 127 }" in body, "never-completed sentinel is not mapped"


def test_invoke_lane_marks_only_the_claude_child_as_a_fleet_lane():
    # KEY-VERDICT-OVERWRITTEN-BY-ROTATION-CHECKPOINT-1. The user-level Stop hook usage-guard.py skips
    # its rotation-checkpoint block when MLV_FLEET_LANE is set; without it a headless lane takes one more
    # turn and --output-format json reports only that turn as `result`, overwriting the verdict.
    body = LANE_RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert (
        "if ($cfg.engine -eq 'claude') {\n    $psi.Environment['MLV_FLEET_LANE'] = '1'\n}" in body
    ), "Invoke-Lane must set MLV_FLEET_LANE=1 on the claude child only; the codex path is unchanged"
    assert body.count("MLV_FLEET_LANE") == 1
    # The variable reaches the hook only if the contained host does not replace the provider's environment.
    start = body.index("$hostSource = @'")
    host = body[start:body.index("\n'@", start)]
    assert ".Environment" not in host, "the contained host must let the provider inherit its environment"
    # NA-3 prefixes: the new name must not be one of them.
    assert not "MLV_FLEET_LANE".startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_"))


def test_invoke_lane_child_processes_open_no_console_window_and_read_utf8():
    # INVOKE-LANE-CREATENOWINDOW-1. These three lines lived only as an uncommitted edit in the canonical
    # checkout, so a checkout or rotation would have silently reverted them.
    body = LANE_RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = body.index("function Invoke-GitCaptureUtf8(")
    git = body[start:body.index("\n}\n", start)]
    assert "$psi.CreateNoWindow = $true" in git, "each git capture must not flash a console window"
    start = body.index("$hostSource = @'")
    host = body[start:body.index("\n'@", start)]
    assert "$p.CreateNoWindow=$true" in host, "the claude child must not open a console window"
    assert "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)" in host, (
        "the host must write UTF-8 without a BOM to the pipe the lane reads"
    )


def test_the_timeout_code_matches_the_taxonomy_the_repo_already_uses():
    # boundedRunnerExitCodes already fixes timeout=124; a second private meaning for the same
    # condition is how two tools end up disagreeing about one event.
    config = json.loads((ROOT / "closeout.config.json").read_text(encoding="utf-8"))
    # The taxonomy lives under `locking`, not at the top level. Found by searching the config
    # rather than assuming: the first version of this test guessed the top level, and a test that
    # skips is a test that never fired.
    codes = config["locking"]["boundedRunnerExitCodes"]
    assert int(codes["timeout"]) == 124
    assert "-1      { 124 }" in LANE_RUNNER.read_text(encoding="utf-8")

DISPATCHER = ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1"


def run_dispatcher(tmp_path, items, *extra):
    """Run the real dispatcher against a synthetic queue fixture, -DryRun and
    -NoLandingProbe so it never calls gh or Invoke-Lane.ps1 for real. Uses -QueuePath,
    which exists precisely so a fixture queue can be substituted (the script refuses to
    ever mutate the canonical one). A successful (non-empty-candidates) run still writes
    a real prompt file under the live repo's .claude-state\\fleet-runs\\prompts\\ -- that
    is the dispatcher's normal side effect even under -DryRun -- so the caller must clean
    up any path reported on a "WORKSTREAM: prompt=" line.
    """
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({"schema": "dual-lane-queue.v1", "note": "test fixture", "items": items}))
    result = subprocess.run(
        [
            "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(DISPATCHER), "-QueuePath", str(queue_path), "-NoLandingProbe", "-DryRun", *extra,
        ],
        text=True, capture_output=True,
    )
    prompt_path = None
    for line in result.stdout.splitlines():
        if line.startswith("WORKSTREAM: prompt="):
            prompt_path = Path(line.split("=", 1)[1].strip())
    if prompt_path and prompt_path.exists():
        prompt_path.unlink()
    return result


def test_booked_card_is_reported_not_dispatched():
    """B2-TOOLING-BASELINE (state=booked) burned a dispatch slot on 2026-09-05: the old
    $Terminal blocklist did not name 'booked', so it was treated as ordinary live work.
    queue.json's own note defines 'booked' as "named trigger, not schedulable". A card in
    that state must be reported via a NOT-SCHEDULABLE line and must never be selected,
    even when it is the only candidate on its track."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        result = run_dispatcher(
            tmp_path,
            [{"id": "TEST-BOOKED-1", "state": "booked", "track": "factory", "priority": 1}],
            "-Track", "factory",
        )
        assert "NOT-SCHEDULABLE card=TEST-BOOKED-1 state=booked" in result.stdout
        assert "WORKSTREAM: track=factory card=TEST-BOOKED-1" not in result.stdout
        assert result.returncode != 0


def test_queued_card_on_a_track_is_still_dispatched():
    """Control for the rule above: a card in a genuinely schedulable state (queued) with
    a real track must still be picked and dry-run dispatched, so the allowlist does not
    over-exclude ordinary live work."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        result = run_dispatcher(
            tmp_path,
            [{"id": "TEST-QUEUED-1", "state": "queued", "track": "factory", "priority": 1}],
            "-Track", "factory",
        )
        assert "WORKSTREAM: NOT-SCHEDULABLE" not in result.stdout
        assert "WORKSTREAM: track=factory card=TEST-QUEUED-1" in result.stdout
        assert result.returncode == 0


def test_untracked_card_is_not_auto_selected():
    """Root cause of the same defect: Get-Track returns 'UNSET' for a card with no track,
    and the default -Track auto pool never filtered by track at all, so an untracked card
    was always a candidate once the tracked pools ran dry. A schedulable-state card with
    no track (queue field) must be reported and skipped under the default auto selection."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        result = run_dispatcher(
            tmp_path,
            [{"id": "TEST-UNTRACKED-1", "state": "queued", "priority": 1}],
        )
        assert "NOT-SCHEDULABLE card=TEST-UNTRACKED-1" in result.stdout
        assert "no-board-track-set" in result.stdout
        assert "WORKSTREAM: track=" not in result.stdout
        assert result.returncode != 0


def test_dispatched_untracked_target_state_is_still_schedulable():
    """'dispatched-untracked-target' is a REAL state already live in the board's queue
    (SIDECAR-COVERAGE-1, track=factory, priority=7, owner=codex) - its own stateReason
    explains that "untracked" means the deliverable is gitignored and has no gate, which
    is unrelated to whether this script's `track` field is set. Excluding this state from
    the allowlist would silently strand a card a lane genuinely still owns, which is the
    exact class of defect this fix exists to remove, just aimed at a different card."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        result = run_dispatcher(
            tmp_path,
            [{"id": "TEST-DUT-1", "state": "dispatched-untracked-target", "track": "factory", "priority": 7}],
            "-Track", "factory",
        )
        assert "WORKSTREAM: NOT-SCHEDULABLE" not in result.stdout
        assert "WORKSTREAM: track=factory card=TEST-DUT-1" in result.stdout
        assert result.returncode == 0


def test_explicit_track_unset_still_dispatches_an_untracked_card():
    """Control for the rule above: Invoke-WorkstreamLoop.ps1 rotates through '-Track UNSET'
    on purpose as its own named track, so an explicit request for the UNSET pool must keep
    working even though the default AUTO pool now excludes it."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        result = run_dispatcher(
            tmp_path,
            [{"id": "TEST-UNTRACKED-2", "state": "queued", "priority": 1}],
            "-Track", "UNSET",
        )
        assert "WORKSTREAM: NOT-SCHEDULABLE" not in result.stdout
        assert "WORKSTREAM: track=UNSET card=TEST-UNTRACKED-2" in result.stdout
        assert result.returncode == 0


# --- queue-derive.ps1: the ref it MEASURES AGAINST must not itself be stale -----------------
# Every finding is "is <sha> an ancestor of -MasterRef". A -MasterRef behind its own upstream does
# not make the tool fail, it makes it QUIETLY WRONG in both directions. Measured 2026-09-05: the
# board's local `master` was 5 behind `fork/master`, so c043f6fc -- the commit that landed
# B2-TOOLING-BASELINE -- was not an ancestor of `master` while being one of `fork/master`. It was
# fast-forwarded by hand at 07:05Z and was behind AGAIN by 13:41Z. A hand fix is not a fix.

def stale_ref_repo(tmp_path):
    """A clone whose local branch is strictly BEHIND its upstream -- the exact live defect."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=origin, check=True)
    (origin / "f.txt").write_text("one")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=origin, check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=clone, check=True)

    # origin moves on; the clone fetches, so origin/main ends up ahead of the clone's own main.
    for n in ("two", "three"):
        (origin / "f.txt").write_text(n)
        subprocess.run(["git", "commit", "-qam", n], cwd=origin, check=True)
    subprocess.run(["git", "fetch", "-q"], cwd=clone, check=True)

    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"schema": "dual-lane-queue.v1", "items": []}))
    return clone, queue


def run_derive(clone, queue, ref, extra_env=None):
    e = dict(os.environ)
    # We run the branch's own copy of the script, which by construction differs from fork/master
    # while the PR is open; that is the currency guard's business, not this test's.
    e["MLV_ALLOW_STALE_TOOLS"] = "1"
    e.pop("MLV_ALLOW_STALE_MASTER_REF", None)
    if extra_env:
        e.update(extra_env)
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(DERIVE), "-RepoRoot", str(clone), "-QueueFile", str(queue),
         "-MasterRef", ref, "-NoLandingProbe"],
        text=True, capture_output=True, env=e,
    )


def test_queue_derive_REFUSES_a_master_ref_that_is_behind_its_upstream(tmp_path):
    clone, queue = stale_ref_repo(tmp_path)
    r = run_derive(clone, queue, "main")
    assert r.returncode == 14, "expected refusal exit 14, got %d: %s%s" % (
        r.returncode, r.stdout, r.stderr)
    assert "STALE MEASURING REF REFUSED" in r.stderr
    assert "2 commit(s) behind" in r.stderr, r.stderr
    # The refusal must be ACTIONABLE, not merely correct: it names the fix and the escape hatch.
    assert "git -C" in r.stderr and "fetch" in r.stderr
    assert "MLV_ALLOW_STALE_MASTER_REF=1" in r.stderr


def test_the_stale_ref_refusal_has_a_working_escape_hatch(tmp_path):
    clone, queue = stale_ref_repo(tmp_path)
    r = run_derive(clone, queue, "main", {"MLV_ALLOW_STALE_MASTER_REF": "1"})
    assert r.returncode == 0, "escape hatch did not let the audit run: %s%s" % (r.stdout, r.stderr)
    assert "NO MISMATCHES" in r.stdout


def test_measuring_against_the_upstream_directly_is_never_refused(tmp_path):
    """The refusal must not fire on the one ref that is current by definition."""
    clone, queue = stale_ref_repo(tmp_path)
    r = run_derive(clone, queue, "origin/main")
    assert r.returncode == 0, "%s%s" % (r.stdout, r.stderr)


def test_a_branch_that_is_AHEAD_of_its_upstream_is_not_refused(tmp_path):
    """Unpushed local work is normal development, NOT the staleness defect. Refusing on it would
    make the tool unusable exactly when someone is building a change to the board."""
    clone, queue = stale_ref_repo(tmp_path)
    subprocess.run(["git", "merge", "-q", "origin/main"], cwd=clone, check=True)
    (clone / "f.txt").write_text("local")
    subprocess.run(["git", "commit", "-qam", "unpushed"], cwd=clone, check=True)
    r = run_derive(clone, queue, "main")
    assert r.returncode == 0, "a branch AHEAD of its upstream was refused: %s%s" % (
        r.stdout, r.stderr)


def test_the_stale_ref_guard_fails_OPEN_when_there_is_no_upstream(tmp_path):
    """Polarity copied from assert-script-currency.ps1: stop only on PROVEN drift. A repo with no
    remote cannot prove anything, and a guard that blocked on 'I could not check' would be worse
    than the bug it prevents."""
    repo = tmp_path / "solo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, check=True)
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps({"schema": "dual-lane-queue.v1", "items": []}))
    r = run_derive(repo, queue, "main")
    assert r.returncode == 0, "guard blocked a repo with no upstream to compare: %s" % r.stderr
    assert "STALE MEASURING REF" not in r.stderr


def test_the_refusal_suggests_a_fix_that_actually_WORKS_when_the_ref_is_checked_out(tmp_path):
    """MEASURED while using this guard on 2026-09-05, minutes after writing it.

    It printed `git fetch fork master:master`. Git REFUSES that with "refusing to fetch into
    branch 'refs/heads/master' checked out at ..." -- and the board root had moved onto master
    hours earlier, so the single command the tool suggested was the one command that could not
    work. A refusal that names an impossible fix is a refusal nobody can act on, which is most of
    the way back to being silent.
    """
    clone, queue = stale_ref_repo(tmp_path)   # 'main' IS checked out in this clone
    r = run_derive(clone, queue, "main")
    assert r.returncode == 14
    assert "merge --ff-only origin/main" in r.stderr, r.stderr
    assert "is CHECKED OUT at that path" in r.stderr, r.stderr
    # The impossible form must not be OFFERED AS A COMMAND. It may still be NAMED in the prose
    # that explains why it is not offered -- an earlier draft of this test failed on exactly that
    # sentence, which is the test being wrong rather than the message.
    commands = [l for l in r.stderr.splitlines() if l.strip().startswith("git -C")]
    assert commands, "the refusal offered no command at all"
    assert not [l for l in commands if "main:main" in l], (
        "still OFFERING the refspec fetch that git refuses for a checked-out branch: %s" % commands
    )


def test_the_refusal_suggests_the_refspec_fetch_when_the_ref_is_NOT_checked_out(tmp_path):
    """The other half of the same decision. With the ref not checked out anywhere, the one-line
    refspec fetch is correct and is the better advice -- it does not touch any working tree."""
    clone, queue = stale_ref_repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, text=True,
                          capture_output=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=clone, check=True)
    r = run_derive(clone, queue, "main")
    assert r.returncode == 14
    assert "fetch origin main:main" in r.stderr, r.stderr
    assert "is CHECKED OUT at that path" not in r.stderr, r.stderr


# --- Invoke-Lane: a PROVIDER REFUSAL is a third outcome, never a completed run ----------
# Incident 2026-09-07T01:28Z (fleet-runs\20260907T012821Z): sol was dispatched for round 3 of
# the PR #79 review and codex refused it in 7.7 s with "You've hit your usage limit ... try
# again at Sep 10th". The receipt said exitCode=1, failure=null, complete=true, state=complete.
# Nothing downstream could tell "refused before any work" from "reviewed and objected", and the
# hub idled for an hour on what was actually an owner-side account rotation.
#
# Round 1 BLOCKER: codex echoes the prompt into stderr, and a text scan over the WHOLE
# transcript misclassified a review that merely quoted the incident. Round 2: BLOCKER x2 --
# (1) a completed review that READS this repo prints "api_error_status 429" into stderr via
# tool output and was classified refused even though it answered; (2) removing the echoed
# prompt line-by-line hid a genuine refusal identical to an echoed line. The fix (see
# lane-provider-refusal.ps1) is structural, never a phrase scan: codex is refused iff stdout
# is EMPTY and, after removing the echoed prompt as ONE BLOCK, a framed `ERROR:` line remains;
# claude is decided by its own JSON envelope fields (is_error / api_error_status), never text.

REFUSAL_HELPER = ROOT / "tools" / "coordination" / "lane-provider-refusal.ps1"

REAL_CODEX_USAGE_LIMIT_STDERR = (
    "2026-09-07T01:28:22.710128Z ERROR codex_models_manager::manager: failed to load models cache\n"
    "OpenAI Codex v0.147.0\n--------\nworkdir: C:\\!Layi Wkspc\\MLV-App\nmodel: gpt-5.6-sol\n"
    "--------\nuser\n# CROSS-FAMILY REVIEW: PR #79 at head 22483b33\n...\n"
    "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to "
    "purchase more credits or try again at Sep 10th, 2026 8:09 PM.\n"
    "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to "
    "purchase more credits or try again at Sep 10th, 2026 8:09 PM.\n"
)

REAL_CODEX_KNOWN_GOOD_STDERR = (
    "OpenAI Codex v0.147.0\n--------\nworkdir: C:\\!Layi Wkspc\\MLV-App\nmodel: gpt-5.6-sol\n"
    "provider: openai\napproval: never\nsandbox: read-only\nreasoning effort: high\n--------\n"
    "user\nReply with exactly: PROBE-OK\ncodex\nPROBE-OK\ntokens used\n22,014\nPROBE-OK\n"
)

# Measured 2026-09-05 on the claude engine, two dispatches of twelve (see Invoke-Lane's exit
# code comment): the JSON envelope carried this and exitCode 1.
REAL_CLAUDE_429_ENVELOPE = (
    '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,'
    '"result":"You\'ve hit your session limit. Try again in 3 hours.","num_turns":0}\n'
)

CLAUDE_SUCCESS_QUOTING_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"ERROR: You\'ve hit your usage limit and api_error_status 429 in the quoted text",'
    '"num_turns":3}\n'
)

# Measured 2026-09-15, fleet-runs\ws-PLAY-COUNTERS-CPU-B-20260915T080318Z: the lane CLI's OAuth token
# expired after an account rotation. exit 1 in 5.9 s, USD 0, and no api_error_status - yet the
# receipt carried providerRefusal=null, so a dead login read as an ordinary incomplete run.
REAL_CLAUDE_OAUTH_EXPIRED_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":null,'
    '"result":"Failed to authenticate: OAuth session expired and could not be refreshed",'
    '"terminal_reason":"api_error","num_turns":1}\n'
)


def test_an_expired_claude_oauth_session_is_a_provider_auth_refusal(tmp_path):
    r = _classify(tmp_path, "", "claude", answer=REAL_CLAUDE_OAUTH_EXPIRED_ENVELOPE)
    assert r is not None and r["kind"] == "provider-auth", r
    assert "owner re-authenticates" in r["remedy"], r
    # Quoting the phrase in a SUCCESSFUL answer is still not a refusal.
    quoting = ('{"type":"result","subtype":"success","is_error":false,'
               '"result":"the log said Failed to authenticate: OAuth session expired","num_turns":2}\n')
    assert _classify(tmp_path, "", "claude", answer=quoting) is None
    # sol PR #117 BLOCKER repro: a status on a SUCCESSFUL envelope must not let quoted text classify.
    quoting_with_status = ('{"type":"result","subtype":"success","is_error":false,"api_error_status":401,'
                           '"result":"Failed to authenticate: OAuth session expired"}\n')
    assert _classify(tmp_path, "", "claude", answer=quoting_with_status) is None
    # ...while the numeric 429 status still classifies on its own.
    status_429 = '{"type":"result","subtype":"success","is_error":false,"api_error_status":429,"result":"ok"}\n'
    r429 = _classify(tmp_path, "", "claude", answer=status_429)
    assert r429 is not None and r429["kind"] == "provider-rate-limit", r429


def _classify(tmp_path, text, engine, prompt="", answer=""):
    src = tmp_path / "lane-stderr.txt"
    src.write_text(text, encoding="utf-8")
    psrc = tmp_path / "lane-prompt.txt"
    psrc.write_text(prompt, encoding="utf-8")
    asrc = tmp_path / "lane-answer.txt"
    asrc.write_text(answer, encoding="utf-8")
    cmd = (
        f". '{REFUSAL_HELPER}'; "
        f"$t = [IO.File]::ReadAllText('{src}'); "
        f"$p = [IO.File]::ReadAllText('{psrc}'); "
        f"$a = [IO.File]::ReadAllText('{asrc}'); "
        f"$r = Get-ProviderRefusal -Text $t -Engine '{engine}' -Prompt $p -Answer $a; "
        "if ($null -eq $r) { 'NULL' } else { $r | ConvertTo-Json -Compress }"
    )
    out = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd],
        text=True, capture_output=True,
    )
    assert out.returncode == 0, out.stderr
    line = out.stdout.strip().splitlines()[-1]
    return None if line == "NULL" else json.loads(line)


def test_provider_refusal_classifies_the_real_codex_usage_limit_stderr(tmp_path):
    r = _classify(tmp_path, REAL_CODEX_USAGE_LIMIT_STDERR, "codex", answer="")
    assert r is not None, "the 2026-09-07 refusal must not read as a completed run"
    assert r["kind"] == "provider-usage-limit"
    assert r["engine"] == "codex"
    assert r["retryAfter"] == "Sep 10th, 2026 8:09 PM"
    assert "usage limit" in r["match"]
    assert "rotates" in r["remedy"], "a usage limit is an owner-side rotation, and the receipt says so"


def test_provider_refusal_is_null_on_a_real_known_good_transcript(tmp_path):
    # The falsifier beside its subject: the probe that proved the rotated account worked.
    # It answered ("PROBE-OK" on stdout), so nothing in stderr is even inspected.
    assert _classify(tmp_path, REAL_CODEX_KNOWN_GOOD_STDERR, "codex", answer="PROBE-OK") is None


def test_provider_refusal_classifies_the_claude_429_session_limit(tmp_path):
    r = _classify(tmp_path, "", "claude", answer=REAL_CLAUDE_429_ENVELOPE)
    assert r is not None
    assert r["kind"] == "provider-usage-limit"
    assert r["retryAfter"] == "3 hours"


def test_provider_refusal_claude_success_envelope_quoting_the_phrase_is_null(tmp_path):
    # sol PR #80 R2 BLOCKER (1): a completed run that merely QUOTES the vocabulary -- in its own
    # result text, and with stderr full of the same phrase -- must not classify as refused. Only
    # the envelope's own is_error / api_error_status fields decide for claude; text is never
    # evidence.
    stderr_full_of_phrase = (
        "api_error_status 429\nYou've hit your usage limit\nERROR: You've hit your usage limit\n"
    ) * 3
    r = _classify(tmp_path, stderr_full_of_phrase, "claude", answer=CLAUDE_SUCCESS_QUOTING_ENVELOPE)
    assert r is None


def test_provider_refusal_claude_survives_diagnostic_noise_around_the_envelope(tmp_path):
    # sol PR #80 R3 MAJOR: whole-string ConvertFrom-Json fails OPEN -- the instant stdout carries
    # ANY non-whitespace diagnostic line before or after the envelope, the old code's single
    # try/catch swallowed a REAL refusal as null. Invoke-Lane harvests raw, unfiltered stdout, so
    # this is reachable whenever anything else writes to stdout alongside --output-format json.
    # sol's own repro, verbatim in shape: a "diagnostic" line before the envelope. The envelope
    # carries "type":"result" -- the real --output-format json shape (sol PR #80 R4 MAJOR below).
    envelope = (
        '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,'
        '"result":"You\'ve hit your session limit"}'
    )
    r = _classify(tmp_path, "", "claude", answer="diagnostic\n" + envelope)
    assert r is not None, "a diagnostic line before the envelope must not hide a real refusal"
    assert r["kind"] == "provider-usage-limit"
    # Noise AFTER the envelope must not hide it either.
    r2 = _classify(tmp_path, "", "claude", answer=envelope + "\ntrailing diagnostic noise")
    assert r2 is not None
    assert r2["kind"] == "provider-usage-limit"


def test_provider_refusal_claude_requires_the_result_envelope_shape_not_any_json_line(tmp_path):
    # sol PR #80 R4 MAJOR (sol's own repro, verbatim in shape): the R3 fix trusted the FIRST
    # parseable JSON-object line, so a JSON-shaped diagnostic carrying its own is_error /
    # api_error_status fields -- printed BEFORE the real envelope -- was picked instead, producing
    # a FALSE refusal even though the run completed successfully. Only a line shaped like the real
    # --output-format json envelope (carries "type":"result") may be treated as the envelope.
    fake_diagnostic = (
        '{"is_error":true,"api_error_status":429,"result":"You have hit your usage limit"}'
    )
    real_envelope = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"ordinary completed answer","num_turns":2}'
    )
    r = _classify(tmp_path, "", "claude", answer=fake_diagnostic + "\n" + real_envelope)
    assert r is None, "a JSON-shaped diagnostic without type:result must never be mistaken for the envelope"
    # Mirror: a benign JSON diagnostic must not hide a later GENUINE refusal envelope either.
    benign_diagnostic = '{"note":"some other tool wrote this JSON line"}'
    real_refusal = (
        '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,'
        '"result":"You\'ve hit your session limit"}'
    )
    r2 = _classify(tmp_path, "", "claude", answer=benign_diagnostic + "\n" + real_refusal)
    assert r2 is not None
    assert r2["kind"] == "provider-usage-limit"


# sol PR #80 round 1 BLOCKER (2026-09-07): codex echoes the prompt into stderr, and the first
# version scanned every line, so a review whose PROMPT quoted the incident classified itself as
# refused. sol PR #80 round 2 BLOCKER (finding 1): a completed review that READS this repo prints
# "api_error_status 429" into stderr via tool-output/citation lines and was classified refused
# even though it answered. Both repros are sol's own, verbatim in shape.
def test_provider_refusal_ignores_prose_that_merely_quotes_a_refusal(tmp_path):
    prompt = (
        "# CROSS-FAMILY REVIEW: PR #80\n"
        "The incident: ERROR: You've hit your usage limit. Visit ... try again at Sep 10th, 2026 8:09 PM.\n"
        "Verify it.\n"
    )
    answer = "Successful review quotes: You've hit your usage limit. Verdict APPROVE.\n"
    stderr = (
        prompt
        + "#   CITE-TXN-1 the receipt recorded api_error_status 429 \"You've hit your session limit\"\n"
        + "ERROR: this line is a reviewer quoting: You've hit your usage limit\n"
    )
    # It answered: the classifier never even reaches the text below, which is exactly why a
    # reviewer's citations and quotations cannot self-classify as a refusal.
    assert _classify(tmp_path, stderr, "codex", prompt=prompt, answer=answer) is None


def test_provider_refusal_ignores_the_echoed_prompt_but_still_sees_a_real_error_line(tmp_path):
    prompt = (
        "# CROSS-FAMILY REVIEW: PR #80\n"
        "The incident: ERROR: You've hit your usage limit. Visit ... try again at Sep 10th, 2026 8:09 PM.\n"
        "Verify it.\n"
    )
    # Echo only: the prompt's ERROR line appears in stderr because codex printed the prompt back.
    # No answer: a genuine refusal never produces stdout.
    echoed = "OpenAI Codex v0.147.0\n--------\nuser\n" + prompt + "codex\n"
    assert _classify(tmp_path, echoed, "codex", prompt=prompt, answer="") is None
    # Echo PLUS a genuine refusal line that is not in the prompt: still a refusal.
    refused = echoed + "ERROR: You've hit your usage limit. Visit x to purchase more credits or try again at Sep 11th, 2026 1:00 AM.\n"
    r = _classify(tmp_path, refused, "codex", prompt=prompt, answer="")
    assert r is not None and r["retryAfter"] == "Sep 11th, 2026 1:00 AM"


def test_provider_refusal_removes_the_echoed_prompt_as_one_block_not_line_by_line(tmp_path):
    # sol PR #80 R2 BLOCKER (finding 2): a genuine refusal line IDENTICAL to an echoed prompt
    # line must still be seen. Removing the prompt line-by-line would delete every occurrence,
    # including the provider's own, real, second one. Removing it as one verbatim block only
    # deletes the FIRST occurrence, leaving a later identical line intact.
    x = "ERROR: You've hit your usage limit. Visit x or try again at Sep 10th, 2026 8:09 PM.\n"
    prompt = x
    stderr_with_second_error = "user\n" + x + "\ncodex\n" + x + "\n"
    r = _classify(tmp_path, stderr_with_second_error, "codex", prompt=prompt, answer="")
    assert r is not None
    assert r["retryAfter"] == "Sep 10th, 2026 8:09 PM"
    # Only the echoed occurrence, once: nothing left after the block is removed.
    stderr_echo_only = "user\n" + x + "\n"
    assert _classify(tmp_path, stderr_echo_only, "codex", prompt=prompt, answer="") is None


def test_provider_refusal_requires_the_provider_frame_not_just_the_phrase(tmp_path):
    # Same words, no ERROR: frame -> not a refusal. With the frame -> refusal. Empty answer
    # required in both cases: a real answer means it ran.
    assert _classify(tmp_path, "you've hit your usage limit\n", "codex", answer="") is None
    assert _classify(tmp_path, "ERROR: you've hit your usage limit\n", "codex", answer="") is not None


def test_provider_refusal_requires_an_empty_answer_even_with_a_framed_line(tmp_path):
    # A non-empty answer means the lane ran and produced something: never a refusal, no matter
    # what the (irrelevant, historical) stderr says.
    assert _classify(tmp_path, "ERROR: you've hit your usage limit\n", "codex", answer="APPROVE") is None


def test_provider_refusal_treats_empty_output_as_no_refusal(tmp_path):
    # Silence is not a refusal; it is the -999/incomplete path, which the receipt already names.
    assert _classify(tmp_path, "", "codex", answer="") is None
    assert _classify(tmp_path, "", "claude", answer="") is None


def test_invoke_lane_records_a_provider_refusal_as_refused_not_complete():
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "lane-provider-refusal.ps1" in body, "Invoke-Lane must dot-source the one classifier"
    assert "Get-ProviderRefusal -Text $stderrText -Answer $stdout" in body, (
        "classification must read the harvested stderr and the harvested answer separately, "
        "never a blended blob (sol PR #80 R1+R2 BLOCKERs)"
    )
    assert "-Prompt $Prompt" in body, "the echoed prompt must be excluded from classification (sol PR #80 R1 BLOCKER)"
    assert "providerRefusal = $providerRefusal" in body, "the receipt must carry the refusal verbatim"
    assert "elseif ($null -ne $providerRefusal) { 'refused' }" in body, "state must have a third value"
    assert "$null -eq $failure -and $null -eq $providerRefusal -and $processEnded -and $workEvidence.workCompleted -eq $true" in body, (
        "complete must be false on refusal, on failure, and without positive work evidence"
    )


# --- Invoke-Lane: `complete` is POSITIVE evidence the work finished, never "the process ended" ---
# Incident 2026-09-14, fleet-runs\ws-PLAY-COUNTERS-CPU-20260914T151951Z: exitCode 1 and this
# envelope (trimmed to the deciding fields), yet the receipt said state=complete, complete=true.
REAL_CLAUDE_MAX_TURNS_ENVELOPE = (
    '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":66,'
    '"stop_reason":"tool_use","terminal_reason":"max_turns","total_cost_usd":4.87,'
    '"errors":["Reached maximum number of turns (65)"]}\n'
)
CLAUDE_SUCCESS_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":false,"num_turns":18,'
    '"stop_reason":"end_turn","terminal_reason":"completed","result":"done"}\n'
)
# Measured on this board: is_error=true while subtype still says success.
CLAUDE_API_ERROR_SUCCESS_SUBTYPE_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":true,"terminal_reason":"api_error","result":"x"}\n'
)


def _work_evidence(tmp_path, engine, answer, exit_code):
    asrc = tmp_path / "lane-answer.txt"
    asrc.write_text(answer, encoding="utf-8")
    cmd = (
        f". '{REFUSAL_HELPER}'; "
        f"$a = [IO.File]::ReadAllText('{asrc}'); "
        f"Get-LaneWorkEvidence -Engine '{engine}' -Answer $a -ExitCode {exit_code} | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd],
        text=True, capture_output=True,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_work_evidence_falsifier_exit_1_max_turns_is_not_complete(tmp_path):
    r = _work_evidence(tmp_path, "claude", REAL_CLAUDE_MAX_TURNS_ENVELOPE, 1)
    assert r["workCompleted"] is False, r
    assert r["subtype"] == "error_max_turns" and r["terminalReason"] == "max_turns", r


def test_work_evidence_max_turns_is_not_complete_even_with_exit_0(tmp_path):
    # The envelope alone must defeat completion: exit code is necessary, never sufficient.
    r = _work_evidence(tmp_path, "claude", REAL_CLAUDE_MAX_TURNS_ENVELOPE, 0)
    assert r["workCompleted"] is False, r
    assert r["reason"] == "envelope-is-error", r


def test_work_evidence_requires_every_success_signal(tmp_path):
    assert _work_evidence(tmp_path, "claude", CLAUDE_SUCCESS_ENVELOPE, 0)["workCompleted"] is True
    assert _work_evidence(tmp_path, "claude", CLAUDE_API_ERROR_SUCCESS_SUBTYPE_ENVELOPE, 0)["workCompleted"] is False
    # One falsifier per remaining conjunct (degraded review PR #111, opus-002 major): each envelope
    # passes every other guard, so deleting exactly that guard turns this red.
    bad_subtype = '{"type":"result","subtype":"error_during_execution","is_error":false,"terminal_reason":"completed"}\n'
    r = _work_evidence(tmp_path, "claude", bad_subtype, 0)
    assert r["workCompleted"] is False and r["reason"] == "subtype-error_during_execution", r
    bad_terminal = '{"type":"result","subtype":"success","is_error":false,"terminal_reason":"max_turns"}\n'
    r = _work_evidence(tmp_path, "claude", bad_terminal, 0)
    assert r["workCompleted"] is False and r["reason"] == "terminal-reason-max_turns", r
    no_is_error = '{"type":"result","subtype":"success","terminal_reason":"completed"}\n'
    r = _work_evidence(tmp_path, "claude", no_is_error, 0)
    assert r["workCompleted"] is False and r["reason"] == "envelope-is-error-absent", r
    # terminal_reason absent on an otherwise-successful envelope is still completion.
    no_terminal = '{"type":"result","subtype":"success","is_error":false}\n'
    assert _work_evidence(tmp_path, "claude", no_terminal, 0)["workCompleted"] is True
    # No envelope on the claude engine is absence of evidence, not completion.
    assert _work_evidence(tmp_path, "claude", "", 0)["reason"] == "no-result-envelope"
    assert _work_evidence(tmp_path, "claude", "not json at all", 0)["workCompleted"] is False
    # codex exposes no envelope: exit 0 is the only observable, and non-zero defeats it.
    assert _work_evidence(tmp_path, "codex", "answer", 0)["workCompleted"] is True
    assert _work_evidence(tmp_path, "codex", "answer", 1)["workCompleted"] is False


def test_invoke_lane_receipt_separates_process_ended_from_complete():
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "processEnded = $processEnded" in body
    assert "complete     = $workCompleted" in body
    assert "workEvidence = $workEvidence" in body
    assert "elseif ($workCompleted) { 'complete' }" in body, "state=complete must require work evidence"
    assert "elseif ($processEnded) { 'ended-incomplete' }" in body


def test_invoke_lane_propagates_a_refusal_as_125_ahead_of_the_child_code():
    # 124 timeout, 127 never-completed, 125 provider refused: three sentinels, three meanings.
    # The refusal clause must come FIRST and break, because PowerShell's switch runs every
    # matching clause and a refusal can arrive with any child exit code.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    i_refused = body.index("{ $null -ne $providerRefusal } { 125; break }")
    i_timeout = body.index("-1      { 124 }")
    assert i_refused < i_timeout


# =============================================================================================
# ASTRA-LANE-1: close the name-keyed edit hole, make Claude-lane effort real, add a read-only
# Astra judgement lane.
# =============================================================================================


def test_invoke_lane_edit_refusal_is_keyed_on_engine_not_lane_name():
    """THE HOLE: the old guard was `$Lane -eq 'sol' -or $Lane -eq 'luna'`, an enumerated list of
    lane NAMES. A new codex row (astra) added to $LANES without also being added to that list
    would silently pass -AllowEdits through to codex exec with workspace-write and no Claude
    hook watching it. Keying on `$LANES[$Lane].engine -eq 'codex'` makes every future codex row
    refuse edits by construction, with nothing left to remember to update."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "$AllowEdits -and $LANES[$Lane].engine -eq 'codex'" in body, (
        "the edit-refusal guard must read the engine out of the lane table, not enumerate names"
    )
    assert "$Lane -eq 'sol' -or $Lane -eq 'luna'" not in body, (
        "the name-enumerated guard is exactly the hole this change closes"
    )


def test_invoke_lane_astra_edit_is_refused_before_any_rundir_is_created(tmp_path):
    cmd = (
        "try { & '%s' -Lane astra -Prompt 'x' -WorkDir '%s' -AllowEdits -AllowedTools 'Read' } "
        "catch { $_.Exception.Message }" % (LANE_RUNNER.as_posix(), tmp_path.as_posix())
    )
    result = run_pwsh_command(cmd)
    assert "codex-lane-never-edits" in (result.stdout + result.stderr), result.stdout + result.stderr
    assert not (tmp_path / ".claude-state" / "fleet-runs").exists(), (
        "a run dir was created before the edit refusal fired -- the refusal must precede any "
        "run-dir or receipt-slot creation"
    )


def test_invoke_lane_sol_edit_refusal_still_works_after_the_engine_rekey(tmp_path):
    # Regression: re-keying the guard from lane name to engine must not change sol/luna's own
    # outcome, only how it is decided.
    cmd = (
        "try { & '%s' -Lane sol -Prompt 'x' -WorkDir '%s' -AllowEdits -AllowedTools 'Read' } "
        "catch { $_.Exception.Message }" % (LANE_RUNNER.as_posix(), tmp_path.as_posix())
    )
    result = run_pwsh_command(cmd)
    assert "codex-lane-never-edits" in (result.stdout + result.stderr), result.stdout + result.stderr


def test_invoke_lane_sonnet_edit_is_not_refused_by_the_codex_only_guard(tmp_path):
    # Control: a claude-engine lane must still reach the (separate) allowlist-required guard,
    # never the codex-only refusal -- proves the rekey did not widen the refusal to every engine.
    cmd = (
        "try { & '%s' -Lane sonnet -Prompt 'x' -WorkDir '%s' -AllowEdits -AllowedTools 'ALL' } "
        "catch { $_.Exception.Message }" % (LANE_RUNNER.as_posix(), tmp_path.as_posix())
    )
    result = run_pwsh_command(cmd)
    blob = result.stdout + result.stderr
    assert "allowlist-required" in blob, blob
    assert "codex-lane-never-edits" not in blob, blob


def test_lane_provider_refusal_classifies_by_engine_not_lane_name():
    """Astra needs no new branch here: Get-ProviderRefusal / Get-RefusalKind take only
    -Engine (never a lane id), and Invoke-Lane.ps1 always calls it with -Engine $cfg.engine.
    A new codex-engine lane is classified identically to sol/luna with zero changes to this
    file -- confirmed rather than assumed, per the packet's own instruction to check which."""
    refusal_body = REFUSAL_HELPER.read_text(encoding="utf-8")
    assert "function Get-RefusalKind" in refusal_body
    assert "$Lane" not in refusal_body, (
        "lane-provider-refusal.ps1 must stay engine-keyed; a lane-name branch here would need "
        "a matching astra arm and silently miss the next new lane too"
    )
    lane_body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "Get-ProviderRefusal -Text $stderrText -Answer $stdout -Engine $cfg.engine -Prompt $Prompt" in lane_body


def test_claude_lane_child_effort_applies_the_resolved_table_effort_not_only_an_override():
    """THE BUG: only an explicit -ReasoningEffort ever reached CLAUDE_CODE_EFFORT_LEVEL, so a
    lane dispatched with no override (the common case) ran at whatever the claude CLI's own
    default effort is -- while the receipt recorded $cfg.effort (the table value) as if it had
    been applied. $cfg.effort already IS the effective value by this point (the table default,
    overridden by -ReasoningEffort when supplied at line ~281), so gating the environment
    variable on $cfg.effort instead of the raw override makes what runs match what is recorded,
    and an explicit override still wins because it already mutated $cfg.effort upstream."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "if ($cfg.engine -eq 'claude' -and $cfg.effort) {" in body, (
        "the environment-variable gate must key on the resolved $cfg.effort, not the raw override"
    )
    assert "$psi.Environment['CLAUDE_CODE_EFFORT_LEVEL'] = $cfg.effort" in body
    assert "$psi.Environment['CLAUDE_CODE_EFFORT_LEVEL'] = $ReasoningEffort" not in body, (
        "the old gate ignored every table-default effort for a lane with no explicit override"
    )
    # the override must still take effect, and must do so BEFORE the environment variable is set
    i_override = body.index("if ($ReasoningEffort) { $cfg.effort = $ReasoningEffort }")
    i_applied = body.index("if ($cfg.engine -eq 'claude' -and $cfg.effort) {")
    assert i_override < i_applied, "an explicit -ReasoningEffort must resolve into $cfg.effort before it is applied"


def test_receipt_effort_field_already_records_the_same_resolved_value_that_is_applied():
    # The receipt's `effort` field reads $cfg.effort -- the same variable now gating the
    # environment variable above -- so a claude lane's receipt can never again claim an effort
    # that was not actually requested of the child process.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "effort       = $cfg.effort" in body


def test_fable_lane_resolves_to_high_effort_by_default():
    # Binding fleet owner ruling, 2026-09-08: Fable spends tokens only on important complex
    # reviews and runs at high effort by default (agents/orchestration-tiering.md).
    # LANE-MODEL-CURRENCY-1 (2026-09-26): the pinned id `claude-fable-5` is replaced by the
    # floating CLI alias `fable`, so the row can never go stale the way a pinned id would.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "fable  = @{ engine = 'claude'; model = 'fable';  effort = 'high';" in body


def test_astra_lane_row_is_a_read_only_high_effort_codex_judgement_lane():
    # LANE-MODEL-CURRENCY-1 (owner ruling 2026-09-26): astra's earlier 'xhigh' effort and
    # pinned `gpt-6-astra` id are both superseded -- every lane, astra included, now runs at
    # 'high' effort (never max/xhigh), and a codex row names a TIER ('astra'), resolved to the
    # current highest-version slug at launch by Resolve-CodexModelTier, never a pinned slug.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "astra  = @{ engine = 'codex';  tier = 'astra';   effort = 'high'; role = 'tie-breaker-design-arbiter' }" in body
    assert "ValidateSet('opus', 'sonnet', 'fable', 'sol', 'luna', 'astra')" in body


def test_lanes_table_pins_no_model_id_and_every_effort_is_high():
    # LANE-MODEL-CURRENCY-1: the defect class this card closes is a version written down
    # somewhere going stale. Every claude row's `model` must be a bare CLI alias (never a
    # `claude-*` id), every codex row must name a `tier` (never a `gpt-*` id), and every row's
    # effort must be exactly 'high' -- never max/xhigh, per the owner's ruling.
    body = LANE_RUNNER.read_text(encoding="utf-8")
    lanes_block = body[body.index("$LANES = @{"):body.index("# Absolute launcher paths")]
    lane_lines = [ln for ln in lanes_block.splitlines() if "engine = " in ln]
    assert len(lane_lines) == 6, "expected six lane rows (opus, sonnet, fable, sol, luna, astra)"
    for line in lane_lines:
        assert not re.search(r"model = 'claude-[a-z0-9.-]+'", line), (
            "a claude-engine row pins a versioned model id instead of a floating alias: %s" % line
        )
        assert not re.search(r"'gpt-[0-9]", line), (
            "a codex-engine row pins a versioned model slug instead of a tier: %s" % line
        )
        assert "effort = 'high'" in line, "every lane row must resolve to 'high' effort: %s" % line
        if "engine = 'claude'" in line:
            assert "model = '" in line and "tier = " not in line
        else:
            assert "tier = '" in line and "model = " not in line


def test_astra_reuses_the_engine_generic_codex_argv_branch_verbatim():
    """No lane-specific branch exists or is needed: the codex argv-build block keys off
    $cfg.engine/$cfg.model/$cfg.effort/$sandbox, all resolved from the SAME table row lookup
    every other codex lane goes through -- so astra gets `-s read-only` (the $AllowEdits-false
    default, since astra is never granted edits) and `-c model_reasoning_effort="high"` by
    construction, the same way sol/luna already do, without a new conditional to test."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    start = body.index("} else {\n    $exe  = $CODEX_EXE")
    end = body.index("# ---------------------------------------------------------------- run, bounded")
    codex_branch = body[start:end]
    assert "$sandbox = if ($AllowEdits) { 'workspace-write' } else { 'read-only' }" in codex_branch
    assert "'-s', $sandbox" in codex_branch
    assert 'model_reasoning_effort=`"{0}`"' in codex_branch and "-f $cfg.effort" in codex_branch
    assert "$Lane -eq" not in codex_branch, (
        "the codex argv build must stay lane-name-agnostic; a per-name branch here is exactly "
        "the pattern that left the edit-refusal guard stale"
    )


def test_xhigh_effort_is_never_assigned_to_a_claude_engine_table_row():
    body = LANE_RUNNER.read_text(encoding="utf-8")
    lanes_block = body[body.index("$LANES = @{"):body.index("# Absolute launcher paths")]
    for line in lanes_block.splitlines():
        if "engine = 'claude'" in line:
            assert "xhigh" not in line, "a claude-engine lane row claims an xhigh effort: %s" % line
    # The table is the only place a lane's effort is authored (line ~119's own comment); the sole
    # runtime path that can still change it is -ReasoningEffort, whose ValidateSet excludes
    # 'xhigh' entirely, so neither a caller override nor a codex-only table value can put 'xhigh'
    # onto a claude lane's child process.
    assert "[ValidateSet('', 'low', 'medium', 'high')]" in body, (
        "the -ReasoningEffort override must not admit 'xhigh' -- that is the only runtime path "
        "that could otherwise push an engine-illegal effort onto a claude lane"
    )


def test_a_real_lane_launch_refuses_any_effort_other_than_high():
    """LANE-MODEL-CURRENCY-1 round 2 (sol blocker 1, owner ruling 2026-09-26): -ReasoningEffort's
    ValidateSet above still syntactically admits 'low'/'medium' (existing containment fixture
    tests rely on that to prove the override mechanically reaches the child), but a REAL launch
    -- $CLAUDE_EXE/$CODEX_EXE unmodified from their own $REAL_CLAUDE_EXE/$REAL_CODEX_EXE twins --
    must now refuse before ever starting a provider process. This pins the refusal's source
    shape; tests/coordination/test_lane_containment.py::test_real_lane_launch_refuses_non_high_effort
    proves it end-to-end against a fixture-forced 'real' launch."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "$REAL_CLAUDE_EXE = Join-Path $env:APPDATA 'npm\\claude.cmd'" in body
    assert "$REAL_CODEX_EXE  = Join-Path $env:APPDATA 'npm\\codex.cmd'" in body
    assert "$ConfiguredExeForLane -eq $RealExeForLane -and $cfg.effort -ne 'high'" in body, (
        "the refusal must be scoped to a launch whose exe was never replaced away from the real one"
    )
    assert 'lane-effort-must-be-high' in body
    # The actual throw is deferred into the main try (so a refusal still yields a normal
    # 'failed' receipt and non-zero exit), and must fire before the dispatch ledger row and
    # before any argv/process-start code for either engine.
    i_refusal_computed = body.index("$LaneEffortMustBeHighRefusal = ")
    i_throw = body.index("if ($LaneEffortMustBeHighRefusal) { throw $LaneEffortMustBeHighRefusal }")
    i_ledger = body.index("DISPATCH LEDGER ROW")
    i_claude_argv = body.index("if ($cfg.engine -eq 'claude') {\n    $exe  = $CLAUDE_EXE")
    assert i_refusal_computed < i_throw < i_ledger < i_claude_argv


def test_codex_selected_model_is_recorded_before_resolved_model_is_ever_proven():
    """LANE-MODEL-CURRENCY-1 round 2 (sol blocker 2): the pre-launch model choice is recorded
    as selectedModel, never resolvedModel, until the child is proven to have run. Pins that
    $ResolvedModel is no longer assigned in the codex tier-resolution block (the exact defect
    sol's repro exercised: Process.Start throwing after the old code had already set
    resolvedModel), and that the receipt exposes selectedModel as its own field."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    assert "$SelectedModel = $codexLaunchModel" in body
    assert "$SelectedModel = $claudeLaunchModel" in body
    assert "$ResolvedModel = $codexLaunchModel" not in body, (
        "resolvedModel must never be assigned from the pre-launch choice before Process.Start"
    )
    assert "selectedModel    = $SelectedModel" in body
    # resolvedModel is filled in for codex only after exit, gated on a clean, non-timed-out,
    # non-empty-output completion -- the minimum evidence bar the card sets.
    i_codex_resolved = body.index("if (-not $timedOut -and $exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($final)) {\n        $ResolvedModel = $SelectedModel\n    }")
    i_wait_for_exit = body.index("$exitCode = if ($timedOut) { -1 } else { $proc.ExitCode }")
    assert i_wait_for_exit < i_codex_resolved, (
        "the exit code must be known (the child proven to have run) before resolvedModel can be filled in"
    )


def test_resolve_codex_model_tier_ok_check_uses_the_strictmode_safe_indexer():
    """CODEX-RESOLVER-UNTYPED-EDGE-1 (fable hardening): the ok-check and error extraction must
    use the `.PSObject.Properties[name]` indexer -- the same idiom this file's own modelUsage
    comment (see test above referencing 'PSObject.Properties[' usage) documents as safe
    unconditionally -- never bare `.ok` dot access or `.PSObject.Properties.Name -contains`,
    both of which throw under this file's `Set-StrictMode -Version Latest` on a resolver result
    that is valid JSON but lacks the expected shape (e.g. '{}' or JSON without 'ok')."""
    body = LANE_RUNNER.read_text(encoding="utf-8")
    fn_start = body.index("function Resolve-CodexModelTier")
    fn_end = body.index("\n}\n", fn_start)
    fn_body = body[fn_start:fn_end]
    assert "$resolverParsed.PSObject.Properties['ok']" in fn_body
    assert "$resolverParsed.PSObject.Properties['error']" in fn_body
    assert "-not $resolverParsed.ok" not in fn_body, (
        "bare dot access on a possibly-absent 'ok' property throws under Set-StrictMode Latest"
    )
    assert "$resolverParsed.PSObject.Properties.Name -contains" not in fn_body, (
        "enumerating .Name over a zero-property PSMemberInfoCollection throws under Set-StrictMode Latest"
    )


def test_astra_is_absent_from_the_timer_reachable_lane_lists():
    """Structural 'the top tier never runs a heartbeat': Invoke-Workstream.ps1 and
    Invoke-WorkstreamLoop.ps1 own SEPARATE -Lane ValidateSets and are explicitly out of scope
    for this change. Leaving them untouched is what keeps astra unreachable from anything on a
    schedule; this test pins that as a fact rather than an assumption."""
    workstream_body = WORKSTREAM.read_text(encoding="utf-8")
    loop_body = LOOP.read_text(encoding="utf-8")
    assert "ValidateSet('opus','sonnet','fable','sol','luna')" in workstream_body
    assert "ValidateSet('', 'opus', 'sonnet', 'fable', 'sol', 'luna')" in loop_body
    assert "astra" not in workstream_body
    assert "astra" not in loop_body


# =============================================================================================
# TOOL-LOOP-PLUMBING-1: install-arg forwarding, kind-based lane resolution, the editing dispatch
# (worktree + composer + reservations + kill-switch recheck), and the pre-dispatch PR-review
# evidence exporter.
# =============================================================================================

LOOP_INSTALL_ARGS = ROOT / "tools" / "coordination" / "loop-install-args.ps1"
COMPOSE_CLI = ROOT / "tools" / "coordination" / "Compose-LanePrompt.ps1"
EXPORT_PR_EVIDENCE = ROOT / "tools" / "coordination" / "Export-PrReviewEvidence.ps1"
TEMPLATE_TEXT = (ROOT / "docs" / "lane-prompts" / "v2" / "product-card-TEMPLATE.md").read_text(encoding="utf-8")


def run_pwsh_command(cmd):
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        text=True, capture_output=True,
    )


# --- loop-install-args.ps1: -Install must persist EVERY schedulable parameter --------------
# UNTIL THIS CARD -Install persisted only four of the loop's own parameters (DailyBudget,
# MaxDispatchesPerCycle, TimeoutSec, StaleHours) and silently dropped -Tracks/-Lane/-AllowEdits,
# so a reinstall reset every one of them to its default -- the same class of defect that made the
# 2026-09-03 budget-flag omission an emergency. Get-InstallArgLine/Resolve-Tracks are pure and
# dot-sourced precisely so they can be tested without running the loop itself.

def call_get_install_arg_line(tracks, lane="", allow_edits=False):
    tracks_literal = "@(" + ",".join("'%s'" % t for t in tracks) + ")"
    cmd = (
        ". '%s'; Get-InstallArgLine -ScriptPath 'X:\\loop.ps1' -DailyBudget 12 "
        "-MaxDispatchesPerCycle 2 -TimeoutSec 1500 -StaleHours 12 -Tracks %s%s%s"
        % (
            LOOP_INSTALL_ARGS.as_posix(),
            tracks_literal,
            " -Lane %s" % lane if lane else "",
            " -AllowEdits" if allow_edits else "",
        )
    )
    return run_pwsh_command(cmd)


def test_install_arg_line_forwards_tracks_lane_and_allowedits():
    result = call_get_install_arg_line(["product", "playback"], lane="sonnet", allow_edits=True)
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert '-Tracks "product,playback"' in line, line
    assert "-Lane sonnet" in line, line
    assert "-AllowEdits" in line, line


def test_install_arg_line_omits_lane_and_allowedits_when_unset():
    result = call_get_install_arg_line(["playback"])
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert '-Tracks "playback"' in line, line
    assert "-Lane" not in line, line
    assert "-AllowEdits" not in line, line


def test_resolve_tracks_splits_a_comma_joined_single_string():
    """A pwsh -File scheduled-task action hands -Tracks back as ONE literal string; Resolve-Tracks
    is what the loop calls immediately after binding $Tracks to undo that."""
    cmd = ". '%s'; (Resolve-Tracks -Tracks @('product,playback')) -join '|'" % LOOP_INSTALL_ARGS.as_posix()
    result = run_pwsh_command(cmd)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "product|playback"


def test_resolve_tracks_leaves_a_genuine_multi_element_array_alone():
    cmd = ". '%s'; (Resolve-Tracks -Tracks @('product','playback')) -join '|'" % LOOP_INSTALL_ARGS.as_posix()
    result = run_pwsh_command(cmd)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "product|playback"


def test_resolve_tracks_does_not_split_a_single_track_that_has_no_comma():
    cmd = ". '%s'; (Resolve-Tracks -Tracks @('UNSET')) -join '|'" % LOOP_INSTALL_ARGS.as_posix()
    result = run_pwsh_command(cmd)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "UNSET"


def test_a_dry_run_cycle_started_from_the_persisted_arg_line_reports_the_resolved_tracks():
    """Proves the round trip through REAL pwsh -File argument binding, not just the pure
    Resolve-Tracks function in isolation: -Tracks "product,playback" arrives at the loop as a
    single-element array and must still resolve and print as two comma-separated tracks.
    -MaxDispatchesPerCycle 0 means no dispatch is attempted regardless of the real board's queue
    state, and the kill switch (armed or not) is checked only AFTER this line is printed.

    MINOR 4 (sol round-1 review): this used to hand-reconstruct the scheduled-task argument list
    directly as a Python literal (-Tracks "product,playback" -Lane sonnet -AllowEdits), never
    actually calling Get-InstallArgLine -- so a regression in the arg-line BUILDER itself (a
    dropped flag, wrong quoting, wrong flag spelling) would not show up here even though this
    test's whole point is the exact-line install/reinstall contract. Now it calls
    Get-InstallArgLine for real, takes its ACTUAL returned string, and parses THAT string into an
    argv list -- exactly what a `pwsh -File` scheduled-task action does with a persisted
    Arguments string -- before starting the -DryRun cycle from it."""
    # -MaxDispatchesPerCycle 0 goes INTO the persisted line itself (not appended afterwards): the
    # loop's own param binder throws "specified more than once" if the same flag appears twice in
    # argv, and 0 makes the per-track dispatch loop break before attempting a single real
    # dispatch against the live board, which -DryRun alone would not prevent (it would still
    # forward -DryRun to up to MaxDispatchesPerCycle real dispatcher invocations).
    get_line_cmd = (
        ". '%s'; Get-InstallArgLine -ScriptPath '%s' -DailyBudget 12 -MaxDispatchesPerCycle 0 "
        "-TimeoutSec 1500 -StaleHours 12 -Tracks @('product','playback') -Lane sonnet -AllowEdits"
        % (LOOP_INSTALL_ARGS.as_posix(), LOOP.as_posix())
    )
    line_result = run_pwsh_command(get_line_cmd)
    assert line_result.returncode == 0, line_result.stdout + line_result.stderr
    arg_line = line_result.stdout.strip()
    assert '-Tracks "product,playback"' in arg_line, arg_line
    assert '-Lane sonnet' in arg_line, arg_line
    assert '-AllowEdits' in arg_line, arg_line

    # shlex with posix=False keeps Windows-style backslash paths intact (no backslash-escape
    # processing) while still respecting quotes for word-splitting; the quote characters
    # themselves are left attached and must be stripped before use as a real argv element,
    # since subprocess.run's list form passes each element through literally (no shell requoting).
    raw_tokens = shlex.split(arg_line, posix=False)
    argv = [t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t for t in raw_tokens]

    result = subprocess.run(
        ["pwsh.exe", *argv, "-DryRun"],
        text=True, capture_output=True, timeout=120,
    )
    assert "LOOP: tracks=product, playback" in result.stdout, (
        "argLine=%r argv=%r\n%s%s" % (arg_line, argv, result.stdout, result.stderr)
    )


# --- kind/owner/scope-based lane resolution (deliverable 2) ---------------------------------

def test_kind_and_owner_resolve_the_lane_to_sonnet():
    """0.18 seeds every product/playback card with kind and owner=sonnet; the dispatcher must
    route it to the sonnet lane with no explicit -Lane, replacing the old needsShell-only
    heuristic for any card that names both fields."""
    with_tmp_result = run_dispatcher_lane_test(
        {"id": "TEST-KIND-1", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1},
        "-Track", "product",
    )
    assert "lane=sonnet" in with_tmp_result.stdout, with_tmp_result.stdout + with_tmp_result.stderr


def test_a_kind_only_card_is_not_selected_by_track_filtering():
    """S82: track SELECTION stays keyed on the queue's own `track` field, never on `kind` -- a
    card that carries kind=product but no track field is UNSET on track (Get-Track only reads
    `track`) and must not be picked by an explicit -Track product, even though its kind matches.
    An explicit non-auto -Track that matches nothing reports NO-LIVE-CARDS on that track (a
    distinct, pre-existing diagnostic from the auto-pool's own no-board-track-set) -- what this
    test actually pins is that the card is never selected, whichever diagnostic explains why."""
    result = run_dispatcher_lane_test(
        {"id": "TEST-KIND-ONLY-1", "state": "queued", "kind": "product", "owner": "sonnet", "priority": 1},
        "-Track", "product",
    )
    assert "WORKSTREAM: track=product card=TEST-KIND-ONLY-1" not in result.stdout, result.stdout + result.stderr
    assert result.returncode != 0


def test_a_recon_scope_prefix_routes_to_luna():
    result = run_dispatcher_lane_test(
        {"id": "TEST-RECON-1", "state": "queued", "track": "factory", "priority": 1,
         "scope": "RECON: survey the CI queue"},
        "-Track", "factory",
    )
    assert "lane=luna" in result.stdout, result.stdout + result.stderr


def test_a_review_scope_prefix_routes_to_fable():
    result = run_dispatcher_lane_test(
        {"id": "TEST-REVIEW-1", "state": "queued", "track": "factory", "priority": 1,
         "scope": "REVIEW: read the plan"},
        "-Track", "factory",
    )
    assert "lane=fable" in result.stdout, result.stdout + result.stderr


def test_explicit_lane_wins_over_kind_based_resolution():
    result = run_dispatcher_lane_test(
        {"id": "TEST-EXPLICIT-1", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1},
        "-Track", "product", "-Lane", "fable",
    )
    assert "lane=fable" in result.stdout, result.stdout + result.stderr


def run_dispatcher_lane_test(item, *extra):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        return run_dispatcher(Path(td), [item], *extra)


# --- deliverable 9 wiring: the dispatcher itself must call the pre-dispatch PR-review evidence
# exporter before a review-lane starts (MAJOR 1, sol round-1 review). The exporter script has its
# own standalone tests further down; these three exercise the DISPATCHER's wiring specifically,
# with a fake exporter shim standing in for the real one -- matching the existing fake-gh shim
# pattern rather than re-testing the exporter in isolation, which is exactly what sol's finding
# says the previous suite was missing (the only reference to the exporter outside its own test
# was in this file, never in a production caller).

FAKE_EXPORTER_SHIM = (
    "param([int]$PrNumber,[string]$RunDir,[string]$RepoRoot,[string]$GhExe)\n"
    "if (-not (Test-Path -LiteralPath $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }\n"
    "Set-Content -LiteralPath (Join-Path $RunDir 'exporter-called.txt') "
    "-Value ($PrNumber.ToString() + '|' + $RunDir)\n"
    "exit 0\n"
)

FAKE_EXPORTER_SHIM_REFUSES = (
    "param([int]$PrNumber,[string]$RunDir,[string]$RepoRoot,[string]$GhExe)\n"
    "Write-Output \"REFUSED: pr-head-drift before=a after=b pr=$PrNumber\"\n"
    "exit 3\n"
)


def test_a_review_lane_dispatch_calls_the_pr_evidence_exporter_before_the_lane_starts(tmp_path):
    """The dispatcher must call Export-PrReviewEvidence.ps1 itself before a review-lane (routed
    here by the REVIEW: scope rule to fable) starts, rather than leaving the lane to call `gh`
    and hit the read-only sandbox's Access-is-denied wall. Run under -DryRun (via
    run_dispatcher_lane_test), so the exporter call must happen BEFORE the dry-run exit -- exactly
    like the generic hosted-evidence export it sits beside."""
    fake_exporter = tmp_path / "fake-exporter.ps1"
    fake_exporter.write_text(FAKE_EXPORTER_SHIM, encoding="ascii")
    result = run_dispatcher_lane_test(
        {"id": "TEST-REVIEW-EVIDENCE-1", "state": "queued", "track": "factory", "priority": 1,
         "scope": "REVIEW: read the plan", "prNumber": 81},
        "-Track", "factory", "-ExporterPath", str(fake_exporter),
    )
    assert "lane=fable" in result.stdout, result.stdout + result.stderr
    # NOT \S+: the real board root is "C:\!Layi Wkspc\MLV-App", which contains a space, and
    # runDir is the last field on its line -- capture to end-of-line, not to the first whitespace.
    export_line = next(
        (l for l in result.stdout.splitlines() if "pre-dispatch review-evidence export" in l), None
    )
    assert export_line, "no export line reported: %s" % result.stdout
    run_dir = Path(export_line.split("runDir=", 1)[1].strip())
    marker = run_dir / "exporter-called.txt"
    try:
        assert marker.exists(), "the dispatcher never invoked the exporter: %s" % result.stdout
        assert marker.read_text(encoding="utf-8").startswith("81|"), marker.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_a_review_lane_dispatch_is_refused_when_the_exporter_refuses(tmp_path):
    """Fail CLOSED, the opposite polarity of the generic hosted-evidence export: unverified PR
    evidence is worse than no review at all, so an exporter refusal must stop the dispatch."""
    fake_exporter = tmp_path / "fake-exporter-fail.ps1"
    fake_exporter.write_text(FAKE_EXPORTER_SHIM_REFUSES, encoding="ascii")
    result = run_dispatcher_lane_test(
        {"id": "TEST-REVIEW-EVIDENCE-2", "state": "queued", "track": "factory", "priority": 1,
         "scope": "REVIEW: read the plan", "prNumber": 81},
        "-Track", "factory", "-ExporterPath", str(fake_exporter),
    )
    assert "REFUSED review-evidence-export-failed" in result.stdout, result.stdout + result.stderr
    assert result.returncode != 0
    assert "WORKSTREAM: track=factory card=TEST-REVIEW-EVIDENCE-2" not in result.stdout


def test_a_review_lane_dispatch_with_no_pr_number_does_not_call_the_exporter(tmp_path):
    """Scoped to cards that actually name a PR: a review-lane card with nothing to bind the
    exporter's mandatory -PrNumber to must not attempt the call at all."""
    fake_exporter = tmp_path / "fake-exporter-unused.ps1"
    fake_exporter.write_text(FAKE_EXPORTER_SHIM, encoding="ascii")
    result = run_dispatcher_lane_test(
        {"id": "TEST-REVIEW-EVIDENCE-3", "state": "queued", "track": "factory", "priority": 1,
         "scope": "REVIEW: read the plan"},
        "-Track", "factory", "-ExporterPath", str(fake_exporter),
    )
    assert "lane=fable" in result.stdout, result.stdout + result.stderr
    assert "pre-dispatch review-evidence export" not in result.stdout, result.stdout


# --- editing dispatch: procedure/worktree/composition/reservations (deliverables 2,3,5,6,7) -

def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_fields_text(card_id, extra_lines=""):
    return (
        "# FIELDS for %s\n"
        "CARD_ID: %s\n"
        "PRIORITY: 1\n"
        "CLIP_OR_NONE: none\n"
        "ALLOWED_PATHS: some/path.cpp\n"
        "DELIVERABLE: do the thing\n"
        "ACCEPTANCE: run the test\n"
        "VERIFY_FIRST: check first\n"
        "%s" % (card_id, card_id, extra_lines)
    )


def write_fields_card(dir_path, card_id, extra_lines=""):
    path = Path(dir_path) / ("fields-%s.md" % card_id)
    path.write_text(write_fields_text(card_id, extra_lines), encoding="ascii")
    return path


def editing_board(tmp_path, with_lane_shim=True):
    root, head = fixture_template(("editing_board", with_lane_shim), lambda r: _build_editing_board(r, with_lane_shim)[1])
    shutil.copytree(root, tmp_path, dirs_exist_ok=True)
    return tmp_path / ".claude-state" / "coordination" / "dual-lane", head


def _build_editing_board(tmp_path, with_lane_shim=True):
    """A throwaway git repo playing the board root: a 'fork/master' ref for baseSha resolution
    and worktree-add, and the dual-lane coordination tree an editing dispatch reads from (the
    composer template, and by default a fake Start-EditingLane.ps1 shim so no real lane is ever
    started by a test)."""
    init_repo(tmp_path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    head = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "update-ref", "refs/remotes/fork/master", head], cwd=tmp_path, check=True)

    dual = tmp_path / ".claude-state" / "coordination" / "dual-lane"
    (dual / "prompts" / "v2").mkdir(parents=True)
    (dual / "prompts" / "v2" / "product-card-TEMPLATE.md").write_text(TEMPLATE_TEXT, encoding="utf-8")
    (dual / "receipts").mkdir(parents=True, exist_ok=True)

    if with_lane_shim:
        shim = dual / "Start-EditingLane.ps1"
        shim.write_text(
            "param([string]$Lane,[string]$PromptFile,[string]$WorkDir,[string]$Card,"
            "[string]$RunDir,[string]$ExtraReadDir,[int]$TimeoutSec)\n"
            "Write-Output ('SHIM: lane=' + $Lane + ' card=' + $Card + ' workDir=' + $WorkDir)\n"
            "exit 0\n",
            encoding="ascii",
        )
    return dual, head


def editing_dispatch_env(tmp_path):
    e = dict(os.environ)
    e["MLV_BOARD_ROOT"] = str(tmp_path)
    return e


def cleanup_lane_worktree(board_root, card_id):
    """Best-effort cleanup: a real -DryRun (or shimmed real) editing dispatch creates then
    removes its own C:\\mlvtmp\\lane-<card>-<ts> worktree, but a failed assertion must not leak
    one if a bug ever leaves it behind."""
    for p in glob.glob(str(Path("C:/mlvtmp") / ("lane-%s-*" % card_id))):
        subprocess.run(["git", "-C", str(board_root), "worktree", "remove", p, "--force"],
                        capture_output=True)
        shutil.rmtree(p, ignore_errors=True)
    subprocess.run(["git", "-C", str(board_root), "worktree", "prune"], capture_output=True)


def run_editing_dispatch(tmp_path, queue_items, card_id, extra=(), dry_run=True):
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({"schema": "test", "items": queue_items}))
    args = [
        "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(WORKSTREAM), "-QueuePath", str(queue_path), "-CardId", card_id,
        "-AllowEdits", "-NoLandingProbe", *extra,
    ]
    if dry_run:
        args.append("-DryRun")
    return subprocess.run(args, text=True, capture_output=True, env=editing_dispatch_env(tmp_path))


def test_editing_dispatch_prints_workdir_lane_and_a_full_basesha(tmp_path):
    """Acceptance: a -DryRun dispatch of an eligible product card prints workDir=, lane=sonnet,
    and baseSha=<40 hex> -- baseSha is fork/master resolved at dispatch time in the board repo."""
    dual, head = editing_board(tmp_path)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-A")
    item = {"id": "TEST-EDIT-A", "state": "queued", "track": "product", "kind": "product",
            "owner": "sonnet", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-A.md",
            "procedureSha256": sha256_of(proc)}
    try:
        result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-A")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "lane=sonnet" in result.stdout, result.stdout
        m = re.search(r"WORKSTREAM: workDir=(\S+)", result.stdout)
        assert m, result.stdout
        m2 = re.search(r"WORKSTREAM: baseSha=([0-9a-f]{40})\b", result.stdout)
        assert m2, result.stdout
        assert m2.group(1) == head
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-A")


def _reuse_card(dual, card_id):
    proc = write_fields_card(dual / "prompts" / "v2", card_id)
    return {"id": card_id, "state": "queued", "track": "product", "kind": "product",
            "owner": "sonnet", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-%s.md" % card_id,
            "procedureSha256": sha256_of(proc)}


def test_editing_dispatch_reuses_a_stale_branch_with_no_unique_commits_and_moves_it_to_base(tmp_path):
    """A branch left by an earlier attempt, sitting on an OLDER commit that is an ancestor of
    baseSha, must not block `worktree add` forever: it is reused and moved to baseSha, so the
    lane starts from the fresh base, never the stale tip."""
    dual, first = editing_board(tmp_path)
    subprocess.run(["git", "branch", "product/TEST-EDIT-REUSE-1", first], cwd=tmp_path, check=True)
    (tmp_path / "seed2.txt").write_text("more\n")
    subprocess.run(["git", "add", "seed2.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=tmp_path, check=True)
    head = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "update-ref", "refs/remotes/fork/master", head], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-REUSE-1")], "TEST-EDIT-REUSE-1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "reusing existing branch product/TEST-EDIT-REUSE-1" in result.stdout, result.stdout
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-REUSE-1") == head
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-REUSE-1")


def test_editing_dispatch_refuses_an_existing_branch_that_carries_work(tmp_path):
    """A branch with a commit NOT in baseSha holds work; the dispatcher must refuse (exit 6)
    and must not move or overwrite the branch."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-REUSE-2"], cwd=tmp_path, check=True)
    (tmp_path / "work.txt").write_text("lane work\n")
    subprocess.run(["git", "add", "work.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "lane work"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-REUSE-2")], "TEST-EDIT-REUSE-2")
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED existing-branch-has-work card=TEST-EDIT-REUSE-2" in result.stdout, result.stdout
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-REUSE-2") == tip
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-REUSE-2")


def test_continuation_dispatches_onto_a_branch_that_carries_stage_one_work(tmp_path):
    """TOOL-DISPATCHER-NO-CONTINUATION-PATH-1. The board's two-stage packet rule (implement+commit,
    then a FRESH lane to verify+ship) was UNDISPATCHABLE through this dispatcher: a stage-2 lane
    starts from stage 1's commit by construction, and that was always refused. Every stage 2
    therefore went direct via Invoke-Lane and wrote no dispatch-attempt row, which is what made
    Test-ProductRatioGuard report dispatchCoverage=PARTIAL - one of the two arms that hold the
    guard RED. With the tip named, the dispatch proceeds and the branch is NOT rewound."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-1"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one work\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-1")],
                                      "TEST-EDIT-CONT-1", extra=("-ContinueFromSha", tip))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CONTINUATION card=TEST-EDIT-CONT-1" in result.stdout, result.stdout
        assert tip in result.stdout, result.stdout
        # The load-bearing assertion: stage 1's commit survived. A continuation that rewound the
        # branch to baseSha would silently discard the very work it exists to build on.
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-CONT-1") == tip
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-1")


def test_continuation_refuses_when_the_branch_moved_since_the_caller_looked(tmp_path):
    """The continuation is content-addressed on purpose. If the branch tip is not the commit the
    caller named, fail closed: continuing onto an unnamed commit is how a dispatch silently builds
    on a stranger's work. A bare -Continue switch could not make this distinction."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-2"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one work\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    stale = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "stage1b.txt").write_text("someone else moved it\n")
    subprocess.run(["git", "add", "stage1b.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "a later commit the caller never saw"], cwd=tmp_path, check=True)
    actual = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-2")],
                                      "TEST-EDIT-CONT-2", extra=("-ContinueFromSha", stale))
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED continuation-sha-mismatch" in result.stdout, result.stdout
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-CONT-2") == actual
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-2")


def test_continuation_refuses_a_branch_that_has_diverged_from_base(tmp_path):
    """A continuation EXTENDS the approved base. A branch cut from an older master carries work
    that was never based on what the board approved, and naming its tip must not launder that."""
    dual, first = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-3", first], cwd=tmp_path, check=True)
    (tmp_path / "old.txt").write_text("work on an old base\n")
    subprocess.run(["git", "add", "old.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "work on an old base"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", first], cwd=tmp_path, check=True)
    (tmp_path / "moved.txt").write_text("master moved on\n")
    subprocess.run(["git", "add", "moved.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "master moved on"], cwd=tmp_path, check=True)
    newbase = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "update-ref", "refs/remotes/fork/master", newbase], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-3")],
                                      "TEST-EDIT-CONT-3", extra=("-ContinueFromSha", tip))
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED continuation-not-descended-from-base" in result.stdout, result.stdout
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-3")


def test_continuation_refuses_when_the_branch_does_not_exist(tmp_path):
    """Naming a tip on a branch that is not there is a caller error, not a fresh dispatch. Falling
    through to 'create the branch at baseSha' would silently turn a continuation into a stage 1 and
    the lane would find none of the work its packet describes."""
    dual, head = editing_board(tmp_path)
    absent = "0" * 40
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-4")],
                                      "TEST-EDIT-CONT-4", extra=("-ContinueFromSha", absent))
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED continuation-branch-absent" in result.stdout, result.stdout
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-4")


def test_continuation_on_a_branch_with_no_extra_commits_still_checks_the_sha(tmp_path):
    """sol PR #122 R1 BLOCKER. The continuation gate was keyed on the branch carrying commits
    beyond baseSha. A -ContinueFromSha whose branch happened to carry NOTHING beyond base fell
    through to the fresh-dispatch arm: the named sha was never compared and `git branch -f` was
    reached from a continuation invocation. A continuation must never be silently downgraded into
    a fresh dispatch, and must never reach the rewind."""
    dual, first = editing_board(tmp_path)
    subprocess.run(["git", "branch", "product/TEST-EDIT-CONT-5", first], cwd=tmp_path, check=True)
    (tmp_path / "seed2.txt").write_text("more\n")
    subprocess.run(["git", "add", "seed2.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=tmp_path, check=True)
    head = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "update-ref", "refs/remotes/fork/master", head], cwd=tmp_path, check=True)
    # The branch sits at an ANCESTOR of baseSha, so it carries nothing beyond base - the exact
    # shape that used to bypass the sha comparison.
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-5")],
                                      "TEST-EDIT-CONT-5", extra=("-ContinueFromSha", "0" * 40))
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED continuation-sha-mismatch" in result.stdout, result.stdout
        # And the rewind must NOT have happened: the branch is still where it was.
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-CONT-5") == first
        assert "reusing existing branch" not in result.stdout, result.stdout
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-5")


def test_continuation_refuses_a_dirty_existing_worktree(tmp_path):
    """sol PR #122 R1 MAJOR. Reuse handed the directory straight to the lane. A tree someone else
    left dirty carries changes the lane did not make and would commit as its own."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-6"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    wt = tmp_path / "wt-cont-6"
    subprocess.run(["git", "worktree", "add", str(wt), "product/TEST-EDIT-CONT-6"],
                   cwd=tmp_path, check=True, capture_output=True)
    (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-6")],
                                      "TEST-EDIT-CONT-6", extra=("-ContinueFromSha", tip))
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED continuation-worktree-dirty" in result.stdout, result.stdout
        assert (wt / "someone-elses-work.txt").exists()
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path,
                       check=False, capture_output=True)
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-6")


# ---------------------------------------------------------------- LANE-HUBCHAT-DETACHED-DISPATCH-1
# A lane that dies mid-edit (2026-10-26 09:09Z, the hub chat's tool tree ended with the Desktop app) leaves tracked changes in its worktree; a dispatcher that then
# refuses the dirty tree must first save that work into the refused run dir. lane-dirty-save.ps1 is the helper, Invoke-Workstream.ps1 calls it before exit 6.
DIRTY_SAVE_LIB = ROOT / "tools" / "coordination" / "lane-dirty-save.ps1"


def test_continuation_dirty_refusal_saves_the_tracked_diff_first_and_still_exits_6(tmp_path):
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-8"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    wt = tmp_path / "wt-cont-8"
    subprocess.run(["git", "worktree", "add", str(wt), "product/TEST-EDIT-CONT-8"], cwd=tmp_path, check=True, capture_output=True)
    (wt / "stage1.txt").write_text("stage one\nHALF-FINISHED EDIT FROM A DEAD LANE\n")
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-8")], "TEST-EDIT-CONT-8", extra=("-ContinueFromSha", tip))
        assert result.returncode == 6 and "REFUSED continuation-worktree-dirty" in result.stdout, result.stdout + result.stderr
        saved = re.search(r"DIRTY_TREE_SAVED card=TEST-EDIT-CONT-8 .*?verified=(\S+) patch=(.+?\.patch) status=(.+?\.status\.txt) reapply=", result.stdout)
        assert saved, result.stdout
        assert saved.group(1) == "reverse-apply-ok", result.stdout
        patch = Path(saved.group(2))
        assert patch.is_file() and "HALF-FINISHED EDIT FROM A DEAD LANE" in patch.read_text(encoding="utf-8"), patch
        assert "stage1.txt" in Path(saved.group(3)).read_text(encoding="ascii")
        assert result.stdout.index("DIRTY_TREE_SAVED") < result.stdout.index("REFUSED continuation-worktree-dirty"), "the save must precede the refusal"
        assert "HALF-FINISHED EDIT FROM A DEAD LANE" in (wt / "stage1.txt").read_text(), "the save must not restore or touch the worktree"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path, check=False, capture_output=True)


def test_continuation_dirty_refusal_sees_an_untracked_orphan_despite_show_untracked_files_no(tmp_path):
    # r4 K5 (opus + sol, pre-existing): the dispatcher's own `git status --porcelain` obeys status.showUntrackedFiles=no, so an untracked orphan skipped both the refusal and the save.
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-9"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    wt = tmp_path / "wt-cont-9"
    subprocess.run(["git", "worktree", "add", str(wt), "product/TEST-EDIT-CONT-9"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=tmp_path, check=True)
    (wt / "orphan.txt").write_text("a dead lane's new file\n")
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-9")], "TEST-EDIT-CONT-9", extra=("-ContinueFromSha", tip))
        assert result.returncode == 6 and "REFUSED continuation-worktree-dirty" in result.stdout and "orphan.txt" in result.stdout, result.stdout + result.stderr
        saved = re.search(r"DIRTY_TREE_SAVED card=TEST-EDIT-CONT-9 .*? status=(.+?\.status\.txt) reapply=", result.stdout)
        assert saved and "orphan.txt" in Path(saved.group(1)).read_text(encoding="utf-8"), result.stdout
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path, check=False, capture_output=True)


DIRTY_SAVE_PWSH = shutil.which("pwsh")
# The saver writes only where it can hold a directory open (Windows); elsewhere it refuses every save (r7), so the save tests run on Windows and dirty_save_posix runs the refusal.
dirty_save_only = pytest.mark.skipif(DIRTY_SAVE_PWSH is None or os.name != "nt", reason="the PowerShell helper needs pwsh, and saves only on Windows")
dirty_save_posix = pytest.mark.skipif(DIRTY_SAVE_PWSH is None or os.name == "nt", reason="the PowerShell helper needs pwsh; the platform refusal is POSIX behaviour")


def dirty_save_pwsh(body):
    return subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command", f". '{DIRTY_SAVE_LIB}'; {body}"], text=True, capture_output=True)


@dirty_save_only
def test_save_dirty_worktree_evidence_round_trips_is_idempotent_and_read_only(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    make_repo(wt)
    (wt / "a.txt").write_text("one\n")
    (wt / "blob.bin").write_bytes(bytes(range(256)))
    git(wt, "add", "a.txt", "blob.bin")
    git(wt, "commit", "-qm", "base")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(wt), str(clone))
    (wt / "a.txt").write_text("one\ntwo\n")
    (wt / "blob.bin").write_bytes(bytes(reversed(range(256))))
    (wt / "untracked.txt").write_text("not in the patch\n")
    before = git(wt, "status", "--porcelain")
    dest = tmp_path / "run"
    dest.mkdir()
    first = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert first.returncode == 0 and first.stdout.startswith("DIRTY_TREE_SAVED card=C1 round=r1 files=3 "), first.stdout + first.stderr
    assert "verified=reverse-apply-ok" in first.stdout
    patches = sorted(dest.glob("dirty-worktree-*.patch"))
    assert len(patches) == 1 and not list(dest.glob("*.tmp"))
    assert "untracked.txt" in patches[0].with_suffix(".status.txt").read_text(encoding="ascii") and "not in the patch" not in patches[0].read_text(encoding="utf-8", errors="replace")
    git(clone, "apply", str(patches[0]))  # byte-exact round trip, binary file included
    assert (clone / "blob.bin").read_bytes() == bytes(reversed(range(256))) and (clone / "a.txt").read_text() == "one\ntwo\n"
    assert git(wt, "status", "--porcelain") == before, "the save must not touch the worktree"
    again = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert again.stdout.startswith("DIRTY_TREE_SAVED") and len(list(dest.glob("dirty-worktree-*.patch"))) == 1, "the same diff must keep one name"


def _dirty_save_repo(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    make_repo(wt)
    (wt / "src").mkdir()
    (wt / "src" / "a.txt").write_text("one\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "base")
    (wt / "src" / "a.txt").write_text("two\n")
    dest = tmp_path / "run"
    dest.mkdir()
    return wt, dest


@dirty_save_only
def test_save_dirty_worktree_evidence_names_non_ascii_untracked_files_and_ignores_diff_config(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "config", "diff.noprefix", "true")  # a user/global setting must not change the patch's path prefix (git apply defaults to -p1)
    git(wt, "config", "diff.mnemonicPrefix", "true")
    (wt / "é.txt").write_text("x\n")
    (wt / "猫.txt").write_text("y\n")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED card=C1") and "verified=reverse-apply-ok" in out.stdout, out.stdout + out.stderr
    patch = next(dest.glob("dirty-worktree-*.patch"))
    status = patch.with_suffix(".status.txt").read_text(encoding="utf-8")
    assert "é.txt" in status and "猫.txt" in status and '"' not in status and "?.txt" not in status, status
    text = patch.read_text(encoding="utf-8")
    assert "--- a/src/a.txt" in text and "+++ b/src/a.txt" in text, text
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(wt), str(clone))
    git(clone, "apply", str(patch))  # the advertised reapply command (plain git apply) must land on src/a.txt
    assert (clone / "src" / "a.txt").read_text() == "two\n"


@dirty_save_only
def test_save_dirty_worktree_evidence_never_reuses_a_name_for_a_different_patch(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    first = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert first.stdout.startswith("DIRTY_TREE_SAVED"), first.stdout + first.stderr
    saved = next(dest.glob("dirty-worktree-*.patch"))
    saved.write_bytes(saved.read_bytes() + b"\n")  # stands in for a different diff that shares the 8-hex name prefix
    second = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert second.stdout.startswith("DIRTY_TREE_SAVED") and "verified=reverse-apply-ok" in second.stdout, second.stdout + second.stderr
    patches = sorted(dest.glob("dirty-worktree-*.patch"))
    assert len(patches) == 2, patches
    assert saved.read_bytes().endswith(b"\n\n") and any(p != saved and "two" in p.read_text(encoding="utf-8") for p in patches)


@dirty_save_only
def test_save_dirty_worktree_evidence_reports_a_failed_status_instead_of_empty_evidence(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "orphan.txt").write_text("z\n")
    git(wt, "config", "status.renames", "bogus")  # makes git status (not git diff) exit non-zero with no stdout
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=git-status-exit-"), out.stdout + out.stderr
    assert not list(dest.glob("*.status.txt"))


@dirty_save_only
def test_save_dirty_worktree_evidence_resolves_a_relative_destination_against_the_caller(tmp_path):
    wt, _ = _dirty_save_repo(tmp_path)
    board = tmp_path / "board"
    (board / "run").mkdir(parents=True)
    (wt / "run").mkdir()  # git -C <wt> would resolve a relative --output here
    out = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command",
                          f". '{DIRTY_SAVE_LIB}'; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '.\\run' -CardName C1 -RoundName r1"],
                         text=True, capture_output=True, cwd=board)
    assert out.stdout.startswith("DIRTY_TREE_SAVED"), out.stdout + out.stderr
    assert len(list((board / "run").glob("dirty-worktree-*.patch"))) == 1 and not list((wt / "run").iterdir())


@dirty_save_only
def test_save_dirty_worktree_evidence_keeps_the_real_diff_staged_work_untracked_names_and_one_line(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / ".gitattributes").write_text("src/a.txt diff=hide\n")
    git(wt, "add", ".gitattributes")
    git(wt, "commit", "-qm", "attrs")
    git(wt, "config", "diff.hide.textconv", "git --version")  # a textconv driver that maps every version to the same text would make the patch empty
    git(wt, "config", "status.showUntrackedFiles", "no")
    (wt / "untracked.txt").write_text("u\n")
    (wt / "src" / "a.txt").write_text("staged\n")
    git(wt, "add", "src/a.txt")
    (wt / "src" / "a.txt").write_text("one\n")  # staged, then edited back to HEAD: diff HEAD is empty, the index still holds the work
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName ('C1' + [char]10 + 'C2') -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED card=C1 C2 ") and len(out.stdout.strip().splitlines()) == 1, out.stdout + out.stderr
    status = next(dest.glob("dirty-worktree-*.status.txt")).read_text(encoding="utf-8")
    assert "untracked.txt" in status, status
    staged = list(dest.glob("dirty-worktree-staged-*.diff"))
    assert len(staged) == 1 and "+staged" in staged[0].read_text(encoding="utf-8"), staged
    (wt / "src" / "a.txt").write_text("edited\n")
    again = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert "verified=reverse-apply-ok" in again.stdout, again.stdout + again.stderr
    assert "+edited" in next(p for p in dest.glob("dirty-worktree-*.patch")).read_text(encoding="utf-8")


STAGED_DIFF_ARGS = ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/"]


def _stage_then_edit_back(wt):
    """Staged work the working tree no longer has: `diff HEAD` is empty, only the index still holds it."""
    (wt / "src" / "a.txt").write_text("staged\n")
    git(wt, "add", "src/a.txt")
    (wt / "src" / "a.txt").write_text("one\n")


@dirty_save_only
def test_save_dirty_worktree_evidence_never_overwrites_an_occupied_staged_name(tmp_path):
    # LANE-HUBCHAT-DETACHED-DISPATCH-1 r3 adversary A1: the staged file used to be skipped by an existence-only test, so an occupied name dropped the new diff.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    _stage_then_edit_back(wt)
    cached = subprocess.run(["git", "-C", str(wt), *STAGED_DIFF_ARGS], capture_output=True, check=True).stdout
    occupied = dest / f"dirty-worktree-staged-{hashlib.sha256(cached).hexdigest()[:8]}.diff"
    occupied.write_bytes(b"X")  # stands in for a different staged diff that shares the 8-hex name prefix
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED"), out.stdout + out.stderr
    assert occupied.read_bytes() == b"X", "an occupied evidence name must never be rewritten"
    staged = sorted(dest.glob("dirty-worktree-staged-*.diff"))
    assert len(staged) == 2 and any(p != occupied and p.read_bytes() == cached for p in staged), staged
    assert not list(dest.glob("*.tmp"))
    again = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert again.stdout.startswith("DIRTY_TREE_SAVED") and len(list(dest.glob("dirty-worktree-staged-*.diff"))) == 2, "the same staged diff must keep one name"


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_when_the_staged_diff_cannot_be_written(tmp_path):
    # r3 adversary A2: a failing `git diff --cached` (here: the staged blob is gone from the object store) used to be swallowed and reported as DIRTY_TREE_SAVED.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    _stage_then_edit_back(wt)
    blob = git(wt, "rev-parse", ":src/a.txt")
    obj = wt / ".git" / "objects" / blob[:2] / blob[2:]
    obj.chmod(0o666)
    obj.unlink()
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=git-staged-diff-exit-"), out.stdout + out.stderr
    assert not list(dest.glob("*.tmp")) and not list(dest.glob("dirty-worktree-staged-*")), "no partial staged evidence may be left under a final name"


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_on_a_short_staged_write_with_exit_zero(tmp_path):
    # r3 adversary A2 (volume quota): even when git reports success, a truncated staged diff must not be reported as saved.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    (wt / "src" / "a.txt").write_text("".join(f"line {i}\n" for i in range(40)))
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "wide")
    (wt / "src" / "a.txt").write_text("".join(f"staged {i}\n" for i in range(40)))
    git(wt, "add", "src/a.txt")
    (wt / "src" / "a.txt").write_text("".join(f"line {i}\n" for i in range(40)))
    # A PowerShell function named git outranks git.exe for the helper's `& git` calls: it runs the real git, then halves the staged diff's output file and keeps git's exit code 0.
    shim = (f"$script:RealGit = '{shutil.which('git')}'; "
            "function git { & $script:RealGit @args; $code = $LASTEXITCODE; "
            "if ($args -contains 'diff' -and $args -contains '--cached') { "
            "$o = @($args | Where-Object { $_ -like '--output=*' })[0]; "
            "if ($o) { $p = $o.Substring(9); $b = [System.IO.File]::ReadAllBytes($p); [System.IO.File]::WriteAllBytes($p, $b[0..([int]($b.Length / 2))]) } }; "
            "$global:LASTEXITCODE = $code }")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=staged-diff-incomplete"), out.stdout + out.stderr
    assert not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_never_overwrites_a_status_snapshot_for_the_same_patch(tmp_path):
    # r3 round-2 adversary A1: the status name follows the patch id, so the same tracked diff with a different untracked set rewrote the first snapshot.
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "orphan.txt").write_text("x\n")
    first = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert first.stdout.startswith("DIRTY_TREE_SAVED"), first.stdout + first.stderr
    (wt / "orphan.txt").unlink()
    (wt / "other.txt").write_text("y\n")
    second = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r2")
    assert second.stdout.startswith("DIRTY_TREE_SAVED"), second.stdout + second.stderr
    statuses = sorted(dest.glob("dirty-worktree-*.status*.txt"))
    texts = sorted(p.read_text(encoding="utf-8") for p in statuses)
    assert len(statuses) == 2 and any("orphan.txt" in t for t in texts) and any("other.txt" in t for t in texts), (statuses, texts)
    assert len(list(dest.glob("dirty-worktree-*.patch"))) == 1, "the identical tracked diff keeps one patch name"
    third = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r3")
    assert third.stdout.startswith("DIRTY_TREE_SAVED") and len(list(dest.glob("dirty-worktree-*.status*.txt"))) == 2, "an identical status snapshot is reused, not duplicated"


def _cut_diff_output_shim(marker):
    # A PowerShell function named git outranks git.exe for the helper's `& git` calls: it runs the real git, then cuts the output file of `diff --cached` / `diff HEAD`
    # right before the first line that starts with `marker` (the patch before it stays whole and well-formed) and keeps git's exit code 0.
    return (f"$script:RealGit = '{shutil.which('git')}'; "
            "function git { & $script:RealGit @args; $code = $LASTEXITCODE; "
            "$o = @($args | Where-Object { $_ -like '--output=*' })[0]; "
            "if ($o -and $args -contains 'diff') { $p = $o.Substring(9); $t = [System.IO.File]::ReadAllText($p); "
            f"$i = $t.IndexOf(\"`n{marker}\"); if ($i -gt 0) {{ [System.IO.File]::WriteAllText($p, $t.Substring(0, $i + 1)) }} }}; "
            "$global:LASTEXITCODE = $code }")


def _truncate_staged_at_second_file_shim(second_marker):
    return _cut_diff_output_shim(f"diff --git a/{second_marker}")


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_when_a_staged_diff_loses_a_whole_file(tmp_path):
    # r3 round-2 adversary A2: a diff cut exactly before the second file header is a valid, applicable one-file patch, so reverse-apply alone cannot see the loss.
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "src" / "b.txt").write_text("one\n")
    git(wt, "add", "src/b.txt")
    git(wt, "commit", "-qm", "b")
    for name in ("a.txt", "b.txt"):
        (wt / "src" / name).write_text("staged-" + name + "\n")
        git(wt, "add", f"src/{name}")
        (wt / "src" / name).write_text("one\n")
    out = dirty_save_pwsh(f"{_truncate_staged_at_second_file_shim('src/b.txt')}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=staged-diff-incomplete"), out.stdout + out.stderr
    assert not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_when_a_staged_diff_loses_its_no_newline_marker(tmp_path):
    # r3 round-3 adversary A1: a cut right before `\ No newline at end of file` keeps every line count, so only a byte-exact comparison against git's own output sees it.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    (wt / "src" / "a.txt").write_bytes(b"two")
    git(wt, "add", "src/a.txt")
    (wt / "src" / "a.txt").write_text("one\n")
    out = dirty_save_pwsh(f"{_cut_diff_output_shim(chr(92) + ' No newline at end of file')}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=staged-diff-incomplete"), out.stdout + out.stderr
    assert not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_ignores_git_environment_that_changes_the_diff_or_the_repo(tmp_path):
    # r3 round-3 adversary A2: GIT_DIFF_OPTS=--unified=0 beats -U3 and made the saved patch unusable for plain git apply; a stray GIT_DIR / GIT_INDEX_FILE would aim the saver at another repo.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    (wt / "src" / "a.txt").write_text("alpha\nbeta\ngamma\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "three lines")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(wt), str(clone))
    (wt / "src" / "a.txt").write_text("alpha\nBETA\ngamma\n")
    git(wt, "add", "src/a.txt")
    other = tmp_path / "other"
    other.mkdir()
    make_repo(other)
    env = {**os.environ, "GIT_DIFF_OPTS": "--unified=0", "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other), "GIT_INDEX_FILE": str(tmp_path / "stray-index")}
    out = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command",
                          f". '{DIRTY_SAVE_LIB}'; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1"],
                         text=True, capture_output=True, env=env)
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and "verified=reverse-apply-ok" in out.stdout, out.stdout + out.stderr
    for saved in (next(dest.glob("dirty-worktree-*.patch")), next(dest.glob("dirty-worktree-staged-*.diff"))):
        assert " alpha\n" in saved.read_text(encoding="utf-8"), saved.name
        fresh = tmp_path / ("clone-" + saved.suffix.lstrip("."))
        git(tmp_path, "clone", "-q", str(clone), str(fresh))
        git(fresh, "apply", str(saved))
        assert (fresh / "src" / "a.txt").read_text() == "alpha\nBETA\ngamma\n", saved.name


@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_gitlink_change_plain_apply_cannot_move(tmp_path):
    # r7 (adversary cd31735a-A2): the patch recorded a changed gitlink, but the advertised plain `git apply` ignores a submodule's commit ids and leaves the pointer where it
    # was, and the reverse-apply check passes without looking at it, so SAVED promised a recovery that silently dropped the change. A tracked diff holding a mode-160000
    # entry is refused before anything is written. diff.ignoreSubmodules=all must not hide it (r3 round-3 adversary A3).
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    git(wt, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    git(wt, "commit", "-qm", "gitlink")
    (wt / "vendor" / "sub").mkdir(parents=True)  # an uninitialised submodule directory
    git(wt, "update-index", "--cacheinfo", f"160000,{'2' * 40},vendor/sub")
    (wt / "src" / "a.txt").write_text("two\n")  # an ordinary edit beside it does not make the save whole
    git(wt, "config", "diff.ignoreSubmodules", "all")
    before = git(wt, "status", "--porcelain")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=gitlink-change-not-saved", (out.stdout, out.stderr)
    assert not list(dest.iterdir()) and git(wt, "status", "--porcelain") == before


@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_staged_only_gitlink_and_one_that_appears_after_the_scan(tmp_path):
    # r7 iter 1 (adversary 6433989c-A1): a file turned into a gitlink only in the index is invisible to the HEAD-to-working-tree scan and was published in the staged diff under
    # SAVED; and a gitlink staged after the scan reached the exported patch. The index is scanned too, and every exported diff is checked again before it is placed.
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "b.txt").write_text("b\n")
    git(wt, "add", "b.txt")
    git(wt, "commit", "-qm", "b")
    git(wt, "checkout", "--", "src/a.txt")
    git(wt, "update-index", "--cacheinfo", f"160000,{'2' * 40},src/a.txt")  # src/a.txt stays an ordinary file in the working tree
    (wt / "b.txt").write_text("bb\n")
    assert "160000" not in git(wt, "diff", "HEAD", "--raw", "--ignore-submodules=none"), "the HEAD scan must not see it, or this is not the reported case"
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=gitlink-change-not-saved", (out.stdout, out.stderr)
    assert not list(dest.iterdir())
    git(wt, "reset", "-q", "--", "src/a.txt")
    # Concurrent variant: the first before-write checkpoint (after the scan, before the export) stages a gitlink at a new path.
    shim = ("$script:Staged = $false; function Invoke-DirtyDestCheckpoint([string]$Stage) { "
            f"if (-not $script:Staged) {{ $script:Staged = $true; & git -C '{wt}' update-index --add --cacheinfo \"160000,{'3' * 40},vendor/late\" }} }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    # The late gitlink is only in the index, so it is caught on the exported staged diff, after the main patch and status were placed: the failure names them (header contract).
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=gitlink-change-not-saved patch="), (out.stdout, out.stderr)
    assert "vendor/late" in git(wt, "ls-files", "-s", "vendor/late") and not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_worktree_whose_config_points_git_elsewhere(tmp_path):
    # r7 iter 1 (adversary 6433989c-A2): a linked worktree whose config.worktree sets core.worktree to another directory still printed an empty --show-prefix, so git read
    # that directory while the holds and containment checks used -Wt; evidence went inside the directory git read. The top level git reports must be -Wt itself.
    main, _ = _dirty_save_repo(tmp_path)
    git(main, "checkout", "--", "src/a.txt")
    wt = tmp_path / "linked"
    git(main, "worktree", "add", "-q", str(wt))
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    (other / "src" / "a.txt").write_text("two\n")
    dest = other / "run"
    dest.mkdir()
    git(wt, "config", "extensions.worktreeConfig", "true")
    git(wt, "config", "--worktree", "core.worktree", str(other).replace("\\", "/"))
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=not-a-worktree-root", (out.stdout, out.stderr)
    assert not list(dest.iterdir())


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_when_the_main_patch_loses_a_whole_file(tmp_path):
    # Same class on the main patch (adversary siblings :41 / :77): a complete one-file prefix used to be reported as verified=reverse-apply-ok.
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "src" / "b.txt").write_text("one\n")
    git(wt, "add", "src/b.txt")
    git(wt, "commit", "-qm", "b")
    (wt / "src" / "a.txt").write_text("edited-a\n")
    (wt / "src" / "b.txt").write_text("edited-b\n")
    out = dirty_save_pwsh(f"{_truncate_staged_at_second_file_shim('src/b.txt')}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=patch-incomplete"), out.stdout + out.stderr
    assert not list(dest.glob("dirty-worktree-*.patch")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_pins_the_patch_shape_against_diff_config(tmp_path):
    # r3 adversary A3: diff.context=0 in a repo or user config made both saved diffs context-free, which plain `git apply` rejects.
    wt = tmp_path / "wt"
    wt.mkdir()
    make_repo(wt)
    (wt / "src").mkdir()
    (wt / "src" / "a.txt").write_text("alpha\nbeta\ngamma\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "base")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(wt), str(clone))
    git(wt, "config", "diff.context", "0")
    git(wt, "config", "diff.interHunkContext", "0")
    git(wt, "config", "color.ui", "always")
    git(wt, "config", "diff.submodule", "log")
    (wt / "src" / "a.txt").write_text("alpha\nBETA\ngamma\n")
    git(wt, "add", "src/a.txt")  # also stage it, so the staged diff is exercised
    dest = tmp_path / "run"
    dest.mkdir()
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and "verified=reverse-apply-ok" in out.stdout, out.stdout + out.stderr
    for saved in (next(dest.glob("dirty-worktree-*.patch")), next(dest.glob("dirty-worktree-staged-*.diff"))):
        text = saved.read_text(encoding="utf-8")
        assert " alpha\n" in text and " gamma\n" in text and "\x1b[" not in text, (saved.name, text)
        fresh = tmp_path / ("clone-" + saved.suffix.lstrip("."))
        git(tmp_path, "clone", "-q", str(clone), str(fresh))
        git(fresh, "apply", str(saved))  # the advertised plain reapply command
        assert (fresh / "src" / "a.txt").read_text() == "alpha\nBETA\ngamma\n", saved.name


@dirty_save_only
def test_save_dirty_worktree_evidence_accepts_every_staged_shape(tmp_path):
    # Every legitimate staged shape (rename, delete, mode change, binary, non-ASCII name) must pass both the byte-exact stream check and the
    # `git apply --cached --check -R` applicability check, and the saved file must re-apply to a clean index. A gitlink is refused instead (r7, see the test above).
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    (wt / "gone.txt").write_text("bye\n")
    (wt / "moved.txt").write_text("".join(f"row {i}\n" for i in range(20)))
    (wt / "mode.sh").write_text("echo hi\n")
    (wt / "bin.dat").write_bytes(bytes(range(256)))
    (wt / "é.txt").write_text("accent\n")
    git(wt, "add", "gone.txt", "moved.txt", "mode.sh", "bin.dat", "é.txt")
    git(wt, "commit", "-qm", "shapes")
    (wt / "é.txt").write_text("accent\nchanged\n")
    git(wt, "add", "é.txt")
    git(wt, "rm", "-q", "gone.txt")
    git(wt, "mv", "moved.txt", "renamed.txt")
    git(wt, "update-index", "--chmod=+x", "mode.sh")
    (wt / "bin.dat").write_bytes(bytes(reversed(range(256))))
    (wt / "fresh.txt").write_text("new file\n")
    (wt / "empty.txt").write_text("")
    git(wt, "add", "bin.dat", "fresh.txt", "empty.txt")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and " staged=none " not in out.stdout, out.stdout + out.stderr
    staged = next(dest.glob("dirty-worktree-staged-*.diff"))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(wt), str(clone))
    git(clone, "reset", "-q", "--hard", "HEAD")
    git(clone, "apply", "--cached", str(staged))
    assert git(clone, "diff", "--cached", "--name-status", "--no-renames") == git(wt, "diff", "--cached", "--name-status", "--no-renames")


ZERO_SHA = "0" * 40


def _leave_a_conflicted_merge(wt):
    """A lane that died mid `git merge fork/master`: HEAD src/a.txt='ours', index stages 1/2/3 for it, a working file with a local edit on top."""
    (wt / "src" / "a.txt").write_text("ours\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "ours")
    # bytes on stdin: text mode would turn every LF into CRLF on Windows and git would read a different blob / a malformed index-info line
    blobs = {s: subprocess.run(["git", "-C", str(wt), "hash-object", "-w", "--stdin"], input=text, capture_output=True, check=True).stdout.decode().strip()
             for s, text in ((1, b"base\n"), (2, b"ours\n"), (3, b"theirs\n"))}
    info = (f"0 {ZERO_SHA}\tsrc/a.txt\n" + "".join(f"100644 {blobs[s]} {s}\tsrc/a.txt\n" for s in (1, 2, 3))).encode()
    subprocess.run(["git", "-C", str(wt), "update-index", "--index-info"], input=info, capture_output=True, check=True)
    (wt / ".git" / "MERGE_HEAD").write_text(git(wt, "rev-parse", "HEAD") + "\n")
    (wt / "src" / "a.txt").write_text("ours\nlocal edit\n")
    assert git(wt, "ls-files", "-u").count("\n") == 2, "the fixture must leave three unmerged stages: " + git(wt, "ls-files", "-s")
    assert subprocess.run(["git", "-C", str(wt), "diff", "--cached"], text=True, capture_output=True).stdout.strip() == "* Unmerged path src/a.txt"


def _assert_unmerged_failure(out, dest, wt):
    # r4 K1 (opus + sol BLOCKER): `git diff --cached` writes only '* Unmerged path src/a.txt' with exit 0, and that was returned as DIRTY_TREE_SAVED staged=<that line>.
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=unmerged-index "), out.stdout + out.stderr
    assert "DIRTY_TREE_SAVED" not in line and len(line.splitlines()) == 1, line
    named = {key: Path(m.group(1)) for key in ("patch", "status", "merge-state") if (m := re.search(rf" {key}=(.+?\.(?:patch|txt))(?: |$)", line))}
    assert set(named) == {"patch", "status", "merge-state"}, line
    assert all(p.is_file() and p.parent == dest for p in named.values()), (named, line)
    state = named["merge-state"].read_text(encoding="utf-8")
    assert git(wt, "rev-parse", "HEAD") in state and "MERGE_HEAD" in state, state
    assert state.count("src/a.txt") == 3 and "\t" in state, state  # the `git ls-files -u` listing: one line per stage
    assert "local edit" in named["patch"].read_text(encoding="utf-8"), "the working-tree edit stays in the patch"
    assert not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp")), sorted(p.name for p in dest.iterdir())


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_and_names_every_file_when_the_index_is_unmerged(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    _leave_a_conflicted_merge(wt)
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    _assert_unmerged_failure(out, dest, wt)
    again = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert again.stdout == out.stdout and len(list(dest.glob("dirty-worktree-merge-state-*.txt"))) == 1, "the same merge state keeps one name"


@dirty_save_only
def test_save_dirty_worktree_evidence_reports_an_unmerged_index_without_a_merge_head_and_under_the_native_exit_preference(tmp_path):
    # Same input as the test above without MERGE_HEAD (a `git checkout -m` / stash-pop conflict), run under the strictest caller preference: the typed line must survive both.
    wt, dest = _dirty_save_repo(tmp_path)
    _leave_a_conflicted_merge(wt)
    (wt / ".git" / "MERGE_HEAD").unlink()
    out = dirty_save_pwsh(f"$PSNativeCommandUseErrorActionPreference = $true; $ErrorActionPreference = 'Stop'; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=unmerged-index "), out.stdout + out.stderr
    state = Path(re.search(r" merge-state=(.+?\.txt)(?: |$)", line).group(1)).read_text(encoding="utf-8")
    assert "MERGE_HEAD=none" in state and state.count("src/a.txt") == 3, state


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_typed_when_a_claimed_staged_diff_does_not_reverse_apply_to_the_index(tmp_path):
    # r4 K1: the header promises "no reverse-apply to the index is never SAVED" for every input. git is shimmed to refuse the applicability check only, so the
    # byte-exact stream check still passes and the typed failure can only come from the `git apply --cached --check -R` result.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    _stage_then_edit_back(wt)
    shim = (f"$script:RealGit = '{shutil.which('git')}'; "
            "function git { if ($args -contains 'apply' -and $args -contains '--cached') { $global:LASTEXITCODE = 1; return }; & $script:RealGit @args }")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=staged-diff-not-applicable "), out.stdout + out.stderr
    assert re.search(r" patch=\S+\.patch", line) and re.search(r" status=\S+\.txt", line), line
    assert not list(dest.glob("dirty-worktree-staged-*")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_checks_applicability_with_the_whitespace_policy_pinned(tmp_path):
    # An apply.whitespace=error setting must not turn a staged line with trailing blanks into a false not-applicable failure (nor verified=reverse-apply-FAILED on the patch).
    wt, dest = _dirty_save_repo(tmp_path)
    # HEAD holds the blank-padded line, so the REVERSED patch (the one the check applies) adds it back.
    (wt / "src" / "a.txt").write_bytes(b"trailing   \n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "padded")
    git(wt, "config", "apply.whitespace", "error")
    git(wt, "config", "core.whitespace", "trailing-space")
    (wt / "src" / "a.txt").write_bytes(b"clean\n")
    git(wt, "add", "src/a.txt")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and "verified=reverse-apply-ok" in out.stdout and " staged=none " not in out.stdout, out.stdout + out.stderr


@dirty_save_only
def test_save_dirty_worktree_evidence_saves_staged_only_work_under_the_native_exit_preference(tmp_path):
    # r4 K2 (sol BLOCKER): with $PSNativeCommandUseErrorActionPreference = $true and $ErrorActionPreference = 'Stop' in the caller, the expected exit 1 of
    # `git diff --cached --quiet` threw and the staged capture was lost. HEAD and working src/a.txt='one', index 'staged'.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    _stage_then_edit_back(wt)
    out = dirty_save_pwsh(f"$PSNativeCommandUseErrorActionPreference = $true; $ErrorActionPreference = 'Stop'; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED card=C1") and " staged=none " not in out.stdout and "verified=empty-diff" in out.stdout, out.stdout + out.stderr
    staged = list(dest.glob("dirty-worktree-staged-*.diff"))
    assert len(staged) == 1 and "+staged" in staged[0].read_text(encoding="utf-8"), staged
    assert not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_restores_nothing_in_the_callers_native_exit_preference(tmp_path):
    # The preference is set for the helper's own calls only; the caller's value is unchanged on return.
    wt, dest = _dirty_save_repo(tmp_path)
    out = dirty_save_pwsh(f"$PSNativeCommandUseErrorActionPreference = $true; $null = Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1; Write-Output \"pref=$PSNativeCommandUseErrorActionPreference\"")
    assert out.stdout.strip() == "pref=True", out.stdout + out.stderr


@dirty_save_only
def test_save_dirty_worktree_evidence_reports_the_real_reason_when_a_move_fails_without_a_name_clash(tmp_path):
    # r4 K3 (opus): a sharing violation / AV lock on the temp file used to be reported as reason=name-collision after four attempts.
    wt, dest = _dirty_save_repo(tmp_path)
    shim = "function Invoke-DirtyFileMove([string]$From, [string]$To) { throw [System.IO.IOException]::new('The process cannot access the file because it is being used by another process.', 0x80070020) }"
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=move-failed:0x80070020"), out.stdout + out.stderr
    assert "name-collision" not in line and len(line.splitlines()) == 1, line
    assert not list(dest.iterdir()), "no temp file may be left behind"


@dirty_save_only
def test_save_dirty_worktree_evidence_still_reports_a_name_collision_as_one_and_names_the_placed_patch(tmp_path):
    # K3 counterpart: a genuine clash (the target exists and holds different bytes at every length) stays name-collision, and the line names the patch already placed.
    wt, dest = _dirty_save_repo(tmp_path)
    shim = ("function Invoke-DirtyFileMove([string]$From, [string]$To) { "
            "if ((Split-Path -Leaf $To) -like 'dirty-worktree-*.status*') { [System.IO.File]::WriteAllText($To, 'other'); throw [System.IO.IOException]::new('exists') }; "
            "[System.IO.File]::Move($From, $To) }")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    patch = next(dest.glob("dirty-worktree-*.patch"))
    assert line == f"DIRTY_TREE_SAVE_FAILED card=C1 reason=name-collision patch={patch}", (line, out.stderr)
    assert all(p.read_text() == "other" for p in dest.glob("dirty-worktree-*.status*.txt")) and not list(dest.glob("*.tmp"))


@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_inside_the_source_worktree(tmp_path):
    # r4 K4 (sol r2-A4): evidence must never be written into the tree it describes (the next status would list it and a later save would capture its own output).
    wt, _ = _dirty_save_repo(tmp_path)
    inside = wt / "run"
    inside.mkdir()
    for spelling in (str(inside), str(wt), str(wt / "src" / ".." / "run"), str(wt).upper() if os.name == "nt" else str(wt)):
        if not Path(spelling).exists():
            continue
        out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{spelling}' -CardName C1 -RoundName r1")
        assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-inside-worktree", (spelling, out.stdout, out.stderr)
    assert not list(inside.iterdir()) and not [p for p in wt.glob("dirty-worktree-*")], "nothing may be written into the source worktree"


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is the Windows spelling of an alias to the worktree")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_that_reaches_the_worktree_through_a_junction(tmp_path):
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    alias = tmp_path / "alias-run"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(wt / "run")], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here: " + made.stderr)
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{alias}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-inside-worktree", out.stdout + out.stderr
    assert not list((wt / "run").iterdir())


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist on Windows volumes only")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_spelled_with_an_8_3_short_name(tmp_path):
    import ctypes
    wt = tmp_path / "a-worktree-with-a-long-name"
    wt.mkdir()
    make_repo(wt)
    (wt / "a-long-run-directory").mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(wt / "a-long-run-directory"), buf, 1024) or buf.value.lower() == str(wt / "a-long-run-directory").lower():
        pytest.skip("this volume has no 8.3 short names")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{buf.value}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-inside-worktree", out.stdout + out.stderr
    assert not list((wt / "a-long-run-directory").iterdir())


@pytest.mark.skipif(os.name != "nt", reason="SUBST is a Windows drive mapping")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_reached_through_a_subst_drive(tmp_path):
    # r4 adversary A1: no managed API reports a SUBST drive, so X:\run was compared as a different tree from the worktree it physically sits in.
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    letter = next((c for c in "XWVUTSRQPONM" if not Path(f"{c}:\\").exists()), None)
    if letter is None or subprocess.run(["subst", f"{letter}:", str(wt)], capture_output=True).returncode != 0:
        pytest.skip("cannot map a SUBST drive here")
    try:
        out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{letter}:\\run' -CardName C1 -RoundName r1")
        assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-inside-worktree", out.stdout + out.stderr
        assert not list((wt / "run").iterdir())
    finally:
        subprocess.run(["subst", f"{letter}:", "/d"], capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is the Windows spelling of a retargetable alias")
@dirty_save_only
def test_save_dirty_worktree_evidence_keeps_writes_bound_to_the_validated_destination_when_a_junction_is_retargeted(tmp_path):
    # r5 (sol r4 blocker): containment was checked once and every later write reopened the -Dest alias, so a junction retargeted into the worktree after the check
    # wrote the evidence inside the tree it describes. The retarget happens inside the containment check itself (the Resolve-DirtyRealPath call that names the
    # worktree), so no sleep race is involved and the seam exists on the pre-fix head too.
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias-run"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here: " + made.stderr)
    before = git(wt, "status", "--porcelain")
    shim = ("$script:RealRR = (Get-Command Resolve-DirtyRealPath -CommandType Function).ScriptBlock; $script:Retargeted = $false; "
            "function Resolve-DirtyRealPath([string]$Path) { $r = & $script:RealRR $Path; "
            f"if (-not $script:Retargeted -and $Path -eq '{wt}') {{ $script:Retargeted = $true; [System.IO.Directory]::Delete('{alias}'); "
            f"$null = New-Item -ItemType Junction -Path '{alias}' -Target '{wt / 'run'}' }}; $r }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{alias}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert not list((wt / "run").iterdir()) and not [p for p in wt.glob("dirty-worktree-*")], ("evidence landed inside the source worktree", line, out.stderr)
    assert git(wt, "status", "--porcelain") == before, "the source worktree must show no new file"
    assert line.startswith("DIRTY_TREE_SAVE_FAILED") or (line.startswith("DIRTY_TREE_SAVED") and list(outside.glob("dirty-worktree-*.patch"))), (line, out.stderr)


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is the Windows spelling of a retargetable alias")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_to_publish_when_the_destination_identity_changed(tmp_path):
    # r5: the validated destination itself is swapped (renamed away, a junction into the worktree put in its place) between the temp write and the publish.
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    dest = tmp_path / "outside"
    dest.mkdir()
    before = git(wt, "status", "--porcelain")
    # Lock-DirtyDirectory is overridden with a hold that holds nothing (and Unlock with a no-op), so the identity check is what has to catch the swap. The fake handles
    # report the identity of the path they were taken on, as a real hold does at the moment it is taken (r6).
    shim = ("$script:Held = @{}; function Lock-DirtyDirectory([string]$Path) { $k = $script:Held.Count + 1; $script:Held[$k] = $Path; $k }; "
            "function Get-DirtyHeldIdentity($Handle) { Get-DirtyDirIdentity $script:Held[$Handle] }; "
            "function Unlock-DirtyDirectory($Handle) { }; $script:Swapped = $false; function Invoke-DirtyDestCheckpoint([string]$Stage) { "
            f"if ($Stage -eq 'before-publish' -and -not $script:Swapped) {{ $script:Swapped = $true; [System.IO.Directory]::Move('{dest}', '{dest}.moved'); "
            f"$null = New-Item -ItemType Junction -Path '{dest}' -Target '{wt / 'run'}' }} }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-identity-changed"), (line, out.stderr)
    assert len(line.splitlines()) == 1 and "DIRTY_TREE_SAVED" not in line
    assert not list((wt / "run").iterdir()) and not [p for p in wt.glob("dirty-worktree-*")], "nothing may be written inside the source worktree"
    assert git(wt, "status", "--porcelain") == before


@pytest.mark.skipif(os.name != "nt", reason="holding a directory open against rename is Windows share-mode behaviour")
@dirty_save_only
def test_save_dirty_worktree_evidence_holds_the_destination_open_so_it_cannot_be_swapped_mid_save(tmp_path):
    # r5 adversary A1: between the guard and the write there is a window no re-check can close; the destination is therefore held open (no FILE_SHARE_DELETE), and the attempt to
    # rename it away mid-save fails, so the write cannot land in a replacement junction (and the temp cleanup cannot delete a same-named file in the source).
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    dest = tmp_path / "outside"
    dest.mkdir()
    shim = ("$script:Tried = $false; function Invoke-DirtyDestCheckpoint([string]$Stage) { "
            f"if ($Stage -eq 'before-publish' -and -not $script:Tried) {{ $script:Tried = $true; try {{ [System.IO.Directory]::Move('{dest}', '{dest}.moved'); $script:Swap = 'swap=ALLOWED' }} catch {{ $script:Swap = 'swap=blocked' }} }} }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1; Write-Output $script:Swap")
    assert "swap=blocked" in out.stdout and "swap=ALLOWED" not in out.stdout, (out.stdout, out.stderr)
    assert "DIRTY_TREE_SAVED card=C1" in out.stdout and list(dest.glob("dirty-worktree-*.patch")) and not Path(str(dest) + ".moved").exists()
    assert not list((wt / "run").iterdir()) and not [p for p in wt.glob("dirty-worktree-*")]
    after = dirty_save_pwsh(f"[System.IO.Directory]::Move('{dest}', '{dest}.after'); Write-Output moved")
    assert "moved" in after.stdout, "the hold must be released when the save returns"


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is the Windows spelling of a retargetable alias")
@dirty_save_only
def test_save_dirty_worktree_evidence_reads_the_resolved_worktree_when_the_wt_junction_is_retargeted(tmp_path):
    # r5 adversary A2: -Wt was only resolved for the containment check, then git read the -Wt alias again; a junction retargeted to another dirty repository made the saver describe
    # (and write into) the wrong tree. The retarget happens in the Resolve-DirtyRealPath call that names the junction, so it exists on the pre-fix head as well.
    wt_a, dest = _dirty_save_repo(tmp_path)
    (tmp_path / "b").mkdir()
    wt_b, _ = _dirty_save_repo(tmp_path / "b")
    (wt_b / "src" / "a.txt").write_text("bee\n")
    lane = tmp_path / "lane"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(lane), str(wt_a)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here: " + made.stderr)
    before_b = git(wt_b, "status", "--porcelain")
    shim = ("$script:RealRR = (Get-Command Resolve-DirtyRealPath -CommandType Function).ScriptBlock; $script:Retargeted = $false; "
            "function Resolve-DirtyRealPath([string]$Path) { $r = & $script:RealRR $Path; "
            f"if (-not $script:Retargeted -and $Path -eq '{lane}') {{ $script:Retargeted = $true; [System.IO.Directory]::Delete('{lane}'); "
            f"$null = New-Item -ItemType Junction -Path '{lane}' -Target '{wt_b}' }}; $r }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{lane}' -Dest '{dest}' -CardName C1 -RoundName r1")
    patches = list(dest.glob("dirty-worktree-*.patch"))
    assert out.stdout.startswith("DIRTY_TREE_SAVED card=C1") and len(patches) == 1, (out.stdout, out.stderr)
    text = patches[0].read_text(encoding="utf-8")
    assert "+two" in text and "+bee" not in text, "the patch must describe the worktree that was validated, not the one the junction was retargeted to"
    assert git(wt_b, "status", "--porcelain") == before_b and not [p for p in wt_b.glob("dirty-worktree-*")]


@pytest.mark.skipif(os.name != "nt", reason="the kernel path API is Windows-only")
@dirty_save_only
def test_get_dirty_kernel_path_resolves_a_path_longer_than_the_first_buffer(tmp_path):
    # r5 adversary A3: GetFinalPathNameByHandle returns the size it needs when the 2048-char buffer is short; the helper used to give up there, so a long path fell back to the managed
    # walk, which cannot see through a SUBST drive.
    deep = Path("\\\\?\\" + str(tmp_path))
    for _ in range(9):
        deep = deep / ("d" * 230)
    try:
        os.makedirs(str(deep))
        out = dirty_save_pwsh(f"$k = Get-DirtyKernelPath '{deep}'; if ($k) {{ Write-Output ('len=' + $k.Length) }} else {{ Write-Output 'len=none' }}")
        assert out.stdout.strip() == f"len={len(str(deep)) - 4}", (out.stdout.strip()[:80], out.stderr[:200])
    finally:
        import shutil
        shutil.rmtree("\\\\?\\" + str(tmp_path / ("d" * 230)), ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="an SMB share spelling is a Windows namespace")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_that_reaches_the_worktree_through_an_smb_share_spelling(tmp_path):
    # r5 adversary A4: \\localhost\C$\... resolves into the share namespace, where no local-prefix comparison relates it to the worktree; the volume serial + file index do.
    wt, _ = _dirty_save_repo(tmp_path)
    (wt / "run").mkdir()
    drive, rest = os.path.splitdrive(str(wt / "run"))
    unc = f"\\\\localhost\\{drive[0]}$" + rest
    if not os.path.isdir(unc):
        pytest.skip("no administrative share on this host")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{unc}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-unc-local-host", (out.stdout, out.stderr)
    assert not list((wt / "run").iterdir())
    # r5 iter 2 (adversary A2): a share of the worktree's own subfolder ends its ancestry at the share root, so no identity walk can relate it; any UNC path whose host is this machine
    # is refused, including one that is not inside the worktree.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    drive, rest = os.path.splitdrive(str(outside))
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '\\\\localhost\\{drive[0]}${rest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-unc-local-host" and not list(outside.iterdir()), (out.stdout, out.stderr)


@pytest.mark.skipif(os.name != "nt", reason="the deep path needs the extended-length prefix, a Windows spelling")
@dirty_save_only
def test_dirty_identity_walk_reaches_the_worktree_however_deep_the_destination_is(tmp_path):
    # r5 iter 2 (adversary A2): the walk up from the destination stopped after 128 parents, short of a worktree further up.
    wt, _ = _dirty_save_repo(tmp_path)
    deep = "\\\\?\\" + str(wt)
    for _ in range(140):
        deep += "\\a"
    try:
        os.makedirs(deep)
        out = dirty_save_pwsh(f"Write-Output (Test-DirtyUnderIdentity -Path '{deep}' -Identity (Get-DirtyDirIdentity '{wt}'))")
        assert out.stdout.strip() == "True", (out.stdout, out.stderr)
    finally:
        import shutil
        shutil.rmtree("\\\\?\\" + str(wt) + "\\a", ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="holding a directory open is Windows share-mode behaviour")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_it_cannot_hold(tmp_path):
    # r5 iter 2 (adversary A5): a failed hold was accepted as 'no hold', leaving the destination swappable for the whole save.
    wt, dest = _dirty_save_repo(tmp_path)
    out = dirty_save_pwsh(f"function Lock-DirtyDirectory([string]$Path) {{ $null }}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-not-held", (out.stdout, out.stderr)
    assert not list(dest.iterdir()) and not [p for p in wt.glob("dirty-worktree-*")]


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_closed_on_a_platform_with_no_directory_hold(tmp_path):
    # r7 (Dot 3d137411, sol r5 blocker 2, adversary a0496049-A1 / cd31735a-A3): off Windows Lock-DirtyDirectory has nothing to hold and returned $null, which was accepted, so a
    # rename after the last guard could steer the diff --output, and the cleanup could follow a replacement. The saver now refuses before any write, reapply line or cleanup.
    # The platform switch is forced here ($IsWindows is read-only, not constant, and $env:OS is what the fallback test reads), so the real Lock-DirtyDirectory takes no hold.
    wt, dest = _dirty_save_repo(tmp_path)
    before = git(wt, "status", "--porcelain")
    off_windows = "Set-Variable -Name IsWindows -Value $false -Scope Global -Force; Remove-Item -LiteralPath Env:OS -ErrorAction SilentlyContinue"
    out = dirty_save_pwsh(f"{off_windows}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=no-directory-hold-on-this-platform", (out.stdout, out.stderr)
    assert not list(dest.iterdir()) and not list(wt.glob("dirty-worktree-*")) and git(wt, "status", "--porcelain") == before


@dirty_save_posix
def test_save_dirty_worktree_evidence_refuses_every_save_on_posix(tmp_path):
    wt, dest = _dirty_save_repo(tmp_path)
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=no-directory-hold-on-this-platform", (out.stdout, out.stderr)
    assert not list(dest.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is the Windows spelling of a retargetable alias")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_worktree_replaced_between_resolving_and_holding_it(tmp_path):
    # r5 iter 2 (adversary A3): the worktree was resolved and then held in two steps; a rename plus a junction in between made the hold pin another repository, which was then saved.
    wt_a, dest = _dirty_save_repo(tmp_path)
    (tmp_path / "b").mkdir()
    wt_b, _ = _dirty_save_repo(tmp_path / "b")
    (wt_b / "src" / "a.txt").write_text("bee\n")
    before_b = git(wt_b, "status", "--porcelain")
    shim = ("$script:RealLock = (Get-Command Lock-DirtyDirectory -CommandType Function).ScriptBlock; $script:Swapped = $false; "
            "function Lock-DirtyDirectory([string]$Path) { "
            f"if ($Path -ieq '{wt_a}' -and -not $script:Swapped) {{ $script:Swapped = $true; [System.IO.Directory]::Move('{wt_a}', '{wt_a}.old'); "
            f"$null = New-Item -ItemType Junction -Path '{wt_a}' -Target '{wt_b}' }}; & $script:RealLock $Path }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt_a}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=worktree-identity-changed", (out.stdout, out.stderr)
    assert not list(dest.iterdir()) and git(wt_b, "status", "--porcelain") == before_b


@pytest.mark.skipif(os.name != "nt", reason="holding a directory open is Windows share-mode behaviour")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_worktree_renamed_away_and_replaced_between_resolving_and_holding_it(tmp_path):
    # r6 (Dot R5-B1, sol r5 blocker 1): two ORDINARY renames (a -> a.old, b -> a) between the resolve and the hold keep the resolved spelling, so the path re-check passed and the
    # first identity sample was already b's: b was exported while a's edits stayed unsaved. The identity is now sampled before the hold and compared with the held handle's.
    wt_a, dest = _dirty_save_repo(tmp_path)
    (tmp_path / "b").mkdir()
    wt_b, _ = _dirty_save_repo(tmp_path / "b")
    (wt_b / "src" / "a.txt").write_text("bee\n")
    for repo in (wt_a, wt_b):  # the destination is outside both repositories and above neither, so its hold cannot obstruct the renames
        assert os.path.commonpath([str(dest), str(repo)]) not in (str(dest), str(repo))
    before_b = git(wt_b, "status", "--porcelain")
    shim = ("$script:RealLock = (Get-Command Lock-DirtyDirectory -CommandType Function).ScriptBlock; $script:Swapped = $false; "
            "function Lock-DirtyDirectory([string]$Path) { "
            f"if ($Path -ieq '{wt_a}' -and -not $script:Swapped) {{ $script:Swapped = $true; [System.IO.Directory]::Move('{wt_a}', '{wt_a}.old'); "
            f"[System.IO.Directory]::Move('{wt_b}', '{wt_a}'); $script:Swap = 'swap=done' }}; & $script:RealLock $Path }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt_a}' -Dest '{dest}' -CardName C1 -RoundName r1; Write-Output $script:Swap")
    lines = out.stdout.strip().splitlines()
    assert lines[-1] == "swap=done", (out.stdout, out.stderr)
    assert lines[0] == "DIRTY_TREE_SAVE_FAILED card=C1 reason=worktree-identity-changed", (out.stdout, out.stderr)
    assert not list(dest.iterdir()), "nothing may be saved from the repository that was swapped in"
    assert git(wt_a, "status", "--porcelain") == before_b and (Path(str(wt_a) + ".old") / "src" / "a.txt").read_text() == "two\n"


@pytest.mark.skipif(os.name != "nt", reason="holding a directory open is Windows share-mode behaviour")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_destination_renamed_away_and_replaced_between_resolving_and_holding_it(tmp_path):
    # r6 sibling of R5-B1 (sol r5 class sweep, :265-267,279): the same two ordinary renames on -Dest; the replacement is still outside the worktree, but it is not the
    # directory that was validated, so the save is refused rather than written somewhere nobody named.
    wt, dest = _dirty_save_repo(tmp_path)
    other = tmp_path / "other-run"
    other.mkdir()
    shim = ("$script:RealLock = (Get-Command Lock-DirtyDirectory -CommandType Function).ScriptBlock; $script:Swapped = $false; "
            "function Lock-DirtyDirectory([string]$Path) { "
            f"if ($Path -ieq '{dest}' -and -not $script:Swapped) {{ $script:Swapped = $true; [System.IO.Directory]::Move('{dest}', '{dest}.old'); "
            f"[System.IO.Directory]::Move('{other}', '{dest}') }}; & $script:RealLock $Path }}")
    out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-identity-changed", (out.stdout, out.stderr)
    assert not list(dest.iterdir()) and not list(Path(str(dest) + ".old").iterdir())


@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_a_resolved_path_that_would_break_the_one_line_result(tmp_path):
    # r6 (sol r5 blocker 3): a POSIX directory name may hold LF; it went raw into patch=, status= and reapply=, so the one-line result became four lines. A resolved -Dest or -Wt
    # holding a line break or other control character is refused before anything is held or written. Windows names cannot hold LF, so the resolver seam supplies one.
    wt, dest = _dirty_save_repo(tmp_path)
    for target, reason in ((dest, "dest-path-control-char"), (wt, "worktree-path-control-char")):
        shim = ("$script:RealRR = (Get-Command Resolve-DirtyRealPath -CommandType Function).ScriptBlock; "
                f"function Resolve-DirtyRealPath([string]$Path) {{ $r = & $script:RealRR $Path; if ($r -ieq '{target}') {{ $r + \"`n5\" }} else {{ $r }} }}")
        out = dirty_save_pwsh(f"{shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
        assert out.stdout.strip() == f"DIRTY_TREE_SAVE_FAILED card=C1 reason={reason}", (target, out.stdout, out.stderr)
        assert not list(dest.iterdir())


@dirty_save_posix  # only a POSIX file system lets a directory name hold LF; the control-character refusal comes before the hold
def test_save_dirty_worktree_evidence_keeps_one_line_for_a_posix_destination_holding_lf(tmp_path):
    wt, _ = _dirty_save_repo(tmp_path)
    dest = tmp_path / "round\n5"
    dest.mkdir()
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-path-control-char", (out.stdout, out.stderr)
    assert not list(dest.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="the console encoding is a Windows console property")
@dirty_save_only
def test_save_dirty_worktree_evidence_keeps_non_ascii_names_under_an_ascii_console_encoding(tmp_path):
    # r5 iter 2 (adversary A4): PowerShell decodes git's output with [Console]::OutputEncoding, so an ASCII console saved 'e-acute.txt' as '??.txt'.
    wt, dest = _dirty_save_repo(tmp_path)
    (wt / "é.txt").write_text("x\n")
    out = dirty_save_pwsh(f"[Console]::OutputEncoding = [System.Text.Encoding]::ASCII; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.startswith("DIRTY_TREE_SAVED"), (out.stdout, out.stderr)
    status = next(dest.glob("dirty-worktree-*.status.txt")).read_text(encoding="utf-8")
    assert "?? é.txt" in status.splitlines() and "??.txt" not in status, status


@dirty_save_only
def test_save_dirty_worktree_evidence_reapply_command_survives_apply_whitespace_error(tmp_path):
    # r5 iter 2 (adversary A1): the saver verifies the patch with --whitespace=nowarn, so the advertised command must carry it too, or apply.whitespace=error refuses trailing blanks.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "config", "apply.whitespace", "error")
    git(wt, "config", "core.whitespace", "trailing-space")
    (wt / "src" / "a.txt").write_bytes(b"two   \n")
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    saved = re.search(r" reapply=(git -C .+)$", out.stdout.strip())
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and saved, (out.stdout, out.stderr)
    git(wt, "checkout", "--", "src/a.txt")
    again = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command", saved.group(1)], text=True, capture_output=True)
    assert again.returncode == 0 and (wt / "src" / "a.txt").read_bytes().replace(b"\r\n", b"\n") == b"two   \n", (saved.group(1), again.stdout, again.stderr)


@dirty_save_only
def test_save_dirty_worktree_evidence_reapply_command_survives_an_inherited_git_work_tree_and_git_dir(tmp_path):
    # r6 adversary A1: the saver clears GIT_WORK_TREE / GIT_DIR for its own calls and puts them back, so the advertised command, pasted into that same environment, read the
    # worktree as a subdirectory of the inherited work tree and skipped src/a.txt with exit 0. The command names its repository and work tree itself.
    wt, dest = _dirty_save_repo(tmp_path)
    for inherited in ({"GIT_WORK_TREE": str(tmp_path)}, {"GIT_WORK_TREE": str(tmp_path), "GIT_DIR": str(wt / ".git")}):
        env = dict(os.environ, **inherited)
        (wt / "src" / "a.txt").write_text("two\n")
        out = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command",
                              f". '{DIRTY_SAVE_LIB}'; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1"], text=True, capture_output=True, env=env)
        saved = re.search(r" reapply=(git -C .+)$", out.stdout.strip())
        assert out.stdout.startswith("DIRTY_TREE_SAVED") and saved, (inherited, out.stdout, out.stderr)
        git(wt, "checkout", "--", "src/a.txt")
        again = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command", saved.group(1)], text=True, capture_output=True, env=env)
        assert again.returncode == 0 and (wt / "src" / "a.txt").read_text().replace("\r\n", "\n") == "two\n", (inherited, saved.group(1), again.stdout, again.stderr)


@pytest.mark.skipif(os.name != "nt", reason="an SMB share spelling is a Windows namespace")
@dirty_save_only
def test_save_dirty_worktree_evidence_refuses_unc_spellings_whose_containment_cannot_be_proven(tmp_path):
    # r6 adversary A2: two shares on one remote server, one a subfolder of the other (ReviewRoot$ = D:\review\repo, ReviewRuns$ = D:\review\repo\run): the prefixes differ and the
    # destination's ancestry ends at its share root, so nothing related it to the worktree and evidence was written inside it. A UNC destination is therefore refused when the
    # worktree is also UNC, and a worktree reached over a share of this machine is refused like such a destination. The remote server is modelled by the local-host seam.
    wt, dest = _dirty_save_repo(tmp_path)
    drive, rest_wt = os.path.splitdrive(str(wt))
    rest_dest = os.path.splitdrive(str(dest))[1]
    unc_wt = f"\\\\localhost\\{drive[0]}$" + rest_wt
    if not os.path.isdir(unc_wt):
        pytest.skip("no administrative share on this host")
    out = dirty_save_pwsh(f"function Test-DirtyUncLocalHost([string]$Path) {{ $false }}; Save-DirtyWorktreeEvidence -Wt '{unc_wt}' -Dest '\\\\127.0.0.1\\{drive[0]}${rest_dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=dest-unc-unproven", (out.stdout, out.stderr)
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{unc_wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=worktree-unc-local-host", (out.stdout, out.stderr)
    assert not list(dest.iterdir())


@dirty_save_only
def test_move_dirty_evidence_returns_a_reason_for_a_digest_it_cannot_name_a_file_after(tmp_path):
    # r6 adversary A3 (advisory): -Full shorter than 8 hex threw from Substring instead of returning the promised hashtable.
    out = dirty_save_pwsh(f"$ErrorActionPreference = 'Stop'; foreach ($f in '', 'x', ('A' * 64)) {{ $r = Move-DirtyEvidenceToFreeName -Tmp '{tmp_path / 'none.tmp'}' -Dir '{tmp_path}' -Stem x -Ext .patch -Full $f; "
                          "Write-Output ('{0}|{1}' -f ($r -is [hashtable]), $r.Reason) }")
    assert out.stdout.split() == ["True|bad-digest"] * 3, (out.stdout, out.stderr)
    # r6 iter 2 adversary A5: '$' also matched before a trailing LF; A4: an empty -Dir threw from Join-Path outside the try.
    out = dirty_save_pwsh("$ErrorActionPreference = 'Stop'; function Invoke-DirtyFileMove([string]$From, [string]$To) { throw [System.IO.IOException]::new('no write') }; "
                          f"foreach ($a in @(@('{tmp_path / 'none.tmp'}', '{tmp_path}', (('a' * 64) + [char]10)), @('', '', ('a' * 64)))) {{ "
                          "$r = Move-DirtyEvidenceToFreeName -Tmp $a[0] -Dir $a[1] -Stem x -Ext .patch -Full $a[2]; Write-Output ('{0}|{1}' -f ($r -is [hashtable]), $r.Reason) }")
    assert out.stdout.split() == ["True|bad-digest", "True|bad-argument"], (out.stdout, out.stderr)


@dirty_save_only
def test_dirty_path_helpers_keep_the_drive_root_separator(tmp_path):
    # r5 adversary A6: 'C:' alone is the drive's current directory; a resolved drive root must stay 'C:\' and still contain what is below it.
    out = dirty_save_pwsh("Write-Output (Format-DirtyResolved 'C:\\'); Write-Output (Test-DirtyPathInside -Inner 'C:\\x' -Outer 'C:\\')") if os.name == "nt" else None
    if out is None:
        pytest.skip("drive roots are a Windows spelling")
    assert out.stdout.split() == ["C:\\", "True"], (out.stdout, out.stderr)


@dirty_save_only
def test_save_dirty_worktree_evidence_names_the_placed_patch_when_a_later_step_throws(tmp_path):
    # r4 adversary A2: a throw after the patch was published (here: hashing a locked status candidate) came back from the general catch without the evidence already on disk.
    wt, dest = _dirty_save_repo(tmp_path)
    first = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert first.stdout.startswith("DIRTY_TREE_SAVED"), first.stdout + first.stderr
    patch = next(dest.glob("dirty-worktree-*.patch"))
    shim = ("$script:RealFH = Get-Command Get-FileHash -CommandType Cmdlet; "
            "function Get-FileHash { if (@($args | Where-Object { \"$_\" -like '*.status.txt' }).Count) { throw [System.IO.IOException]::new('locked by another process') }; & $script:RealFH @args }")
    out = dirty_save_pwsh(f"$ErrorActionPreference = 'Stop'; {shim}; Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    line = out.stdout.strip()
    assert line.startswith("DIRTY_TREE_SAVE_FAILED card=C1 reason=") and "locked by another process" in line, out.stdout + out.stderr
    assert line.endswith(f" patch={patch}"), line


@dirty_save_only
def test_save_dirty_worktree_evidence_reads_a_relative_worktree_against_the_powershell_location(tmp_path):
    # r4 adversary A2-1: GetFullPath('.') used the process directory while the caller's Set-Location named the worktree, so an external parent destination was refused as inside it.
    wt, _ = _dirty_save_repo(tmp_path)
    out = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command",
                          f". '{DIRTY_SAVE_LIB}'; Set-Location '{wt}'; Save-DirtyWorktreeEvidence -Wt '.' -Dest '{tmp_path}' -CardName C1 -RoundName r1"],
                         text=True, capture_output=True, cwd=tmp_path.parent)
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and "verified=reverse-apply-ok" in out.stdout, out.stdout + out.stderr
    assert len(list(tmp_path.glob("dirty-worktree-*.patch"))) == 1 and not list(wt.glob("dirty-worktree-*"))


@pytest.mark.skipif(os.name != "nt", reason="the paths below use a dollar sign in a Windows directory name")
@dirty_save_only
def test_save_dirty_worktree_evidence_reapply_command_survives_dollar_signs_and_quotes_in_paths(tmp_path):
    # r4 adversary A2-2: the advertised command was built inside double quotes, so C:\work\$lane expanded a variable and addressed another path.
    base = tmp_path / "it's $lane"
    base.mkdir()
    wt = base / "w$lane"
    wt.mkdir()
    make_repo(wt)
    (wt / "src").mkdir()
    (wt / "src" / "a.txt").write_text("one\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-qm", "base")
    (wt / "src" / "a.txt").write_text("two\n")
    dest = base / "r$round"
    dest.mkdir()
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{str(wt).replace(chr(39), chr(39) * 2)}' -Dest '{str(dest).replace(chr(39), chr(39) * 2)}' -CardName C1 -RoundName r1")
    saved = re.search(r" reapply=(git -C .+)$", out.stdout.strip())
    assert out.stdout.startswith("DIRTY_TREE_SAVED") and saved, out.stdout + out.stderr
    git(wt, "checkout", "--", "src/a.txt")
    again = subprocess.run([DIRTY_SAVE_PWSH, "-NoProfile", "-NonInteractive", "-Command", saved.group(1)], text=True, capture_output=True)
    assert again.returncode == 0 and (wt / "src" / "a.txt").read_text().replace("\r\n", "\n") == "two\n", (saved.group(1), again.stdout, again.stderr)


def _gitlink_checked_out_elsewhere(wt):
    """A submodule recorded at one commit in HEAD and the index but checked out at another."""
    git(wt, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    git(wt, "commit", "-qm", "gitlink")
    sub = wt / "vendor" / "sub"
    sub.mkdir(parents=True)
    make_repo(sub)
    git(wt, "config", "diff.ignoreSubmodules", "all")


@dirty_save_only
def test_save_dirty_worktree_evidence_sees_a_submodule_checked_out_at_another_commit(tmp_path):
    # diff.ignoreSubmodules=all must not hide it; since r7 the gitlink change is refused rather than saved under a reapply command that would drop it.
    wt, dest = _dirty_save_repo(tmp_path)
    git(wt, "checkout", "--", "src/a.txt")
    _gitlink_checked_out_elsewhere(wt)
    out = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{dest}' -CardName C1 -RoundName r1")
    assert out.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=gitlink-change-not-saved", (out.stdout, out.stderr)
    assert not list(dest.iterdir())


def test_continuation_dirty_refusal_sees_a_submodule_checked_out_at_another_commit(tmp_path):
    # r4 adversary A3: diff.ignoreSubmodules=all made `git status` empty, so the dirty branch was skipped, nothing was saved and the lane reused the tree.
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-10"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    wt = tmp_path / "wt-cont-10"
    subprocess.run(["git", "worktree", "add", str(wt), "product/TEST-EDIT-CONT-10"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "diff.ignoreSubmodules", "all"], cwd=tmp_path, check=True)
    make_repo(wt / "vendor" / "sub")
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-10")], "TEST-EDIT-CONT-10", extra=("-ContinueFromSha", tip))
        assert result.returncode == 6 and "REFUSED continuation-worktree-dirty" in result.stdout and "vendor/sub" in result.stdout, result.stdout + result.stderr
        assert "DIRTY_TREE_SAVE_FAILED card=TEST-EDIT-CONT-10 reason=gitlink-change-not-saved" in result.stdout, result.stdout  # r7: refused, not saved without the pointer
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path, check=False, capture_output=True)


@dirty_save_only
def test_save_dirty_worktree_evidence_fails_soft_and_refuses_a_non_root_directory(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    make_repo(wt)
    (wt / "sub").mkdir()
    dest = tmp_path / "run"
    dest.mkdir()
    gone = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt}' -Dest '{tmp_path / 'missing'}' -CardName C1")
    assert gone.returncode == 0 and gone.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=no-run-dir", gone.stdout + gone.stderr
    sub = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{wt / 'sub'}' -Dest '{dest}' -CardName C1")
    assert sub.stdout.strip() == "DIRTY_TREE_SAVE_FAILED card=C1 reason=not-a-worktree-root", sub.stdout + sub.stderr
    plain = tmp_path / "plain"
    plain.mkdir()
    nogit = dirty_save_pwsh(f"Save-DirtyWorktreeEvidence -Wt '{plain}' -Dest '{dest}' -CardName C1")
    assert nogit.returncode == 0 and nogit.stdout.startswith("DIRTY_TREE_SAVE_FAILED card=C1"), nogit.stdout + nogit.stderr
    assert not list(dest.iterdir()), "a failed save must leave nothing behind"


def test_continuation_reuses_a_clean_existing_worktree(tmp_path):
    """The arm none of the round-1 tests exercised: the branch IS checked out somewhere. Stage 1's
    tree carries its build outputs, and making stage 2 rebuild from scratch is most of why the wide
    packets ran out of turns - so a clean, on-tip worktree must be reused, not duplicated."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-CONT-7"], cwd=tmp_path, check=True)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    subprocess.run(["git", "add", "stage1.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "stage one"], cwd=tmp_path, check=True)
    tip = git(tmp_path, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    wt = tmp_path / "wt-cont-7"
    subprocess.run(["git", "worktree", "add", str(wt), "product/TEST-EDIT-CONT-7"],
                   cwd=tmp_path, check=True, capture_output=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-CONT-7")],
                                      "TEST-EDIT-CONT-7", extra=("-ContinueFromSha", tip))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "reusing-worktree=" in result.stdout, result.stdout
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-CONT-7") == tip
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path,
                       check=False, capture_output=True)
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-CONT-7")


def test_a_real_refused_dispatch_leaves_a_typed_attempt_receipt(tmp_path):
    """2026-09-14: ~90 PLAY-COUNTERS-CPU run dirs held only lane-prompt.md because a pre-launch
    refusal reached stdout alone. A NON-dry-run refusal must leave dispatch-attempt.json naming
    the cause, and no lane may have started (no lane receipt)."""
    dual, head = editing_board(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "product/TEST-EDIT-ATTEMPT"], cwd=tmp_path, check=True)
    (tmp_path / "work.txt").write_text("lane work\n")
    subprocess.run(["git", "add", "work.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "lane work"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=tmp_path, check=True)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-ATTEMPT")], "TEST-EDIT-ATTEMPT",
                                      dry_run=False)
        assert result.returncode == 6, result.stdout + result.stderr
        run_dirs = glob.glob(str(tmp_path / ".claude-state" / "fleet-runs" / "ws-TEST-EDIT-ATTEMPT-*"))
        assert len(run_dirs) == 1, run_dirs
        attempt = json.loads((Path(run_dirs[0]) / "dispatch-attempt.json").read_text(encoding="utf-8"))
        assert attempt["schema"] == "mlv-app/workstream-dispatch-attempt/v1"
        assert attempt["outcome"] == "refused-before-launch", attempt
        assert attempt["cause"] == "existing-branch-has-work", attempt
        assert attempt["exitCode"] == 6 and attempt["laneReceipts"] == [], attempt
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-ATTEMPT")


def test_a_terminating_error_after_the_run_dir_is_named_still_leaves_an_attempt_receipt(tmp_path):
    """sol PR #111 R1: a THROW is an exit path too. A procedure path that is a directory passes
    Test-Path and then makes Get-FileHash throw - after $runDir is named, before any refusal."""
    dual, head = editing_board(tmp_path)
    (dual / "prompts" / "v2" / "fields-TEST-EDIT-THROW.md").mkdir(parents=True)
    item = {"id": "TEST-EDIT-THROW", "state": "queued", "track": "product", "kind": "product",
            "owner": "sonnet", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-THROW.md",
            "procedureSha256": "0" * 64}
    try:
        result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-THROW", dry_run=False)
        assert result.returncode != 0, result.stdout + result.stderr
        run_dirs = glob.glob(str(tmp_path / ".claude-state" / "fleet-runs" / "ws-TEST-EDIT-THROW-*"))
        assert len(run_dirs) == 1, (run_dirs, result.stdout, result.stderr)
        attempt = json.loads((Path(run_dirs[0]) / "dispatch-attempt.json").read_text(encoding="utf-8"))
        assert attempt["outcome"] == "refused-before-launch" and attempt["cause"] == "unhandled-error", attempt
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-THROW")


def test_every_workstream_exit_after_the_run_dir_is_named_writes_an_attempt_receipt():
    """Structural: once $runDir is named, every exit (DryRun included) has a Write-DispatchAttempt
    call within the three lines above it (a worktree-cleanup call may sit between), throws are
    covered by a guarded trap, and both launch paths record 'launched' before and after.
    Behavioural tests cover the real refusal and throw paths; this is the cheap net for new exits."""
    lines = WORKSTREAM.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("$runDir = Join-Path"))
    unreceipted = []
    for i in range(start, len(lines)):
        if re.match(r"^\s*exit\b", lines[i]):
            # The receipt comes first; at most a cleanup call and its status line may sit between.
            window = [l.strip() for l in lines[i - 3:i]]
            if any("Write-DispatchAttempt" in l for l in window) or "WORKSTREAM: dispatched" in window[-1]:
                continue
            unreceipted.append((i + 1, window[-1]))
    assert not unreceipted, unreceipted
    body = "\n".join(lines)
    assert "if ($DryRun) { return }" not in body
    assert body.count("-Outcome 'dry-run-not-launched'") == 2
    assert "-Cause 'unhandled-error'" in body and re.search(r"^trap \{", body, re.M)
    assert body.count("-Outcome 'launching' -Cause 'lane-starting'") == 2
    assert body.count("-Outcome 'launched' -Cause 'lane-returned'") == 2
    assert "-Outcome 'launched' -Cause 'lane-starting'" not in body
    # sol PR #111 post-merge: 'launched' only after the child returned. Each LaneLaunched assignment
    # must directly follow the $LASTEXITCODE capture inside the launch try, never precede the call.
    launched_at = [i for i, l in enumerate(lines) if l.strip() == "$script:LaneLaunched = $true"]
    assert len(launched_at) == 2, launched_at
    for i in launched_at:
        assert lines[i - 1].strip() == "$laneExit = $LASTEXITCODE", (i + 1, lines[i - 1])
    assert body.count("$script:LaneStarting = $true") == 2
    assert "elseif ($script:LaneStarting) { 'launch-unconfirmed' }" in body


def _extract_ps_function(tmp_path, source, name):
    extractor = tmp_path / ("extract-%s.ps1" % name)
    extractor.write_text(
        "param($Source,$Name)\n$tokens=$null;$errors=$null\n"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)\n"
        "if($errors.Count){throw 'parse failed'}\n"
        "$f=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name},$true))\n"
        "if($f.Count -ne 1){throw 'expected one function'}\n$f[0].Extent.Text\n", encoding="utf-8")
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(extractor), "-Source", str(source), "-Name", name],
                         text=True, capture_output=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_an_unwritable_run_dir_spools_a_typed_attempt_receipt(tmp_path):
    """sol PR #111 post-merge: a failed dispatch-attempt.json write was only printed. With the run
    dir unwritable (its parent is a FILE), the receipt must land in the fixed spool instead."""
    fn = _extract_ps_function(tmp_path, WORKSTREAM, "Write-DispatchAttempt")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    harness = tmp_path / "spool-harness.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$RepoRoot='" + str(tmp_path).replace("'", "''") + "'\n"
        "$runDir='" + str(blocker / "ws-CARD-1-T").replace("'", "''") + "'\n"
        "$cardId='CARD-1';$cardTrack='product';$Lane='sonnet';$engine='claude';$AllowEdits=$true\n"
        + fn + "\nWrite-DispatchAttempt -Outcome 'refused-before-launch' -Cause 'worktree-add-failed' -ExitCode 3\n",
        encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dispatch-attempt receipt NOT written" in result.stdout and "spooled=" in result.stdout, result.stdout
    spooled = list((tmp_path / ".claude-state" / "fleet-runs" / "dispatch-attempt-spool").glob("CARD-1-*-refused-before-launch.json"))
    assert len(spooled) == 1, spooled
    rec = json.loads(spooled[0].read_text(encoding="utf-8"))
    assert rec["cause"] == "worktree-add-failed" and rec["exitCode"] == 3 and rec["runDirWriteError"], rec


def test_the_loop_carries_receipt_write_failures_into_its_cycle_receipt(tmp_path):
    extractor = tmp_path / "extract-loop.ps1"
    extractor.write_text("param($Source)\n$tokens=$null;$errors=$null\n"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)\n"
        "if($errors.Count){throw 'loop parse failed'}\n"
        "$loops=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.ForEachStatementAst] -and $node.Variable.VariablePath.UserPath -eq 'track'},$true))\n"
        "if($loops.Count -ne 1){throw 'expected one track loop'}\n$loops[0].Extent.Text\n", encoding="utf-8")
    extracted = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(extractor), "-Source", str(LOOP_SCRIPT)], text=True, capture_output=True)
    assert extracted.returncode == 0, extracted.stderr
    dispatcher = tmp_path / "fake-dispatcher.ps1"
    dispatcher.write_text("param($Track)\nWrite-Output ('WORKSTREAM: track=' + $Track + ' card=C1')\n"
                          "Write-Output 'WORKSTREAM: dispatch-attempt receipt NOT written (disk full) runDir=X spool ALSO failed (disk full)'\nexit 0\n", encoding="ascii")
    harness = tmp_path / "loop-body.ps1"
    harness.write_text("$Tracks=@('product')\n$dispatched=@()\n$skipped=@()\n$receiptWriteFailures=@()\n$MaxDispatchesPerCycle=1\n$DailyBudget=9\n$spentToday=0\n$Dispatcher='"
        + str(dispatcher).replace("'", "''") + "'\n$TimeoutSec=1\n$StaleHours=1\n$Lane=''\n$AllowEdits=$false\n$DryRun=$false\n"
        + extracted.stdout + "\n[ordered]@{dispatched=$dispatched;receiptWriteFailures=$receiptWriteFailures}|ConvertTo-Json -Depth 6 -Compress\n", encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    failures = payload["receiptWriteFailures"]
    if isinstance(failures, dict):
        failures = [failures]
    assert len(failures) == 1 and "disk full" in failures[0]["line"] and failures[0]["track"] == "product", payload
    assert "receiptWriteFailures = $receiptWriteFailures" in LOOP_SCRIPT.read_text(encoding="utf-8")


def test_editing_dispatch_creates_a_new_branch_when_none_exists(tmp_path):
    dual, head = editing_board(tmp_path)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, "TEST-EDIT-REUSE-3")], "TEST-EDIT-REUSE-3")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "reusing existing branch" not in result.stdout
        assert git(tmp_path, "rev-parse", "refs/heads/product/TEST-EDIT-REUSE-3") == head
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-REUSE-3")


def test_two_editing_dispatches_get_two_distinct_worktree_paths(tmp_path):
    dual, head = editing_board(tmp_path)
    proc_a = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-B1")
    proc_b = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-B2")
    items = [
        {"id": "TEST-EDIT-B1", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1,
         "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-B1.md",
         "procedureSha256": sha256_of(proc_a)},
        {"id": "TEST-EDIT-B2", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1,
         "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-B2.md",
         "procedureSha256": sha256_of(proc_b)},
    ]
    workdirs = []
    try:
        for card_id in ("TEST-EDIT-B1", "TEST-EDIT-B2"):
            result = run_editing_dispatch(tmp_path, items, card_id)
            assert result.returncode == 0, result.stdout + result.stderr
            m = re.search(r"WORKSTREAM: workDir=(\S+)", result.stdout)
            assert m, result.stdout
            workdirs.append(m.group(1))
        assert workdirs[0] != workdirs[1]
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-B1")
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-B2")


def test_editing_dispatch_refuses_a_codex_lane(tmp_path):
    editing_board(tmp_path)
    item = {"id": "TEST-EDIT-CODEX-1", "state": "queued", "track": "product", "priority": 1}
    result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-CODEX-1", extra=("-Lane", "luna"))
    assert result.returncode == 6, result.stdout + result.stderr
    assert "REFUSED codex-lane-never-edits" in result.stdout


def test_editing_dispatch_refuses_a_card_with_no_procedure(tmp_path):
    editing_board(tmp_path)
    item = {"id": "TEST-EDIT-NOPROC-1", "state": "queued", "track": "product", "priority": 1}
    result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-NOPROC-1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert "REFUSED procedure-missing-or-drifted" in result.stdout


def test_editing_dispatch_refuses_a_drifted_procedure_sha(tmp_path):
    dual, head = editing_board(tmp_path)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-DRIFT-1")
    item = {"id": "TEST-EDIT-DRIFT-1", "state": "queued", "track": "product", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-DRIFT-1.md",
            "procedureSha256": "0" * 64}
    result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-DRIFT-1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert "REFUSED procedure-missing-or-drifted" in result.stdout
    assert sha256_of(proc) != "0" * 64


def test_editing_dispatch_refuses_an_unknown_field(tmp_path):
    dual, head = editing_board(tmp_path)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-UNK-1", extra_lines="FOO: a stray field\n")
    item = {"id": "TEST-EDIT-UNK-1", "state": "queued", "track": "product", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-UNK-1.md",
            "procedureSha256": sha256_of(proc)}
    result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-UNK-1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert "REFUSED unknown-field" in result.stdout


def read_reservation_rows(dual):
    path = dual / "receipts" / "dispatch-reservations.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_real_editing_dispatch_reserves_before_it_starts_and_charges_after(tmp_path):
    """S76: a 'reserved' row is written BEFORE the process launches and a 'charged' row after,
    sharing one reservationId -- the loop counts only 'reserved' rows for today's spend."""
    dual, head = editing_board(tmp_path)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-RES-1")
    item = {"id": "TEST-EDIT-RES-1", "state": "queued", "track": "product", "kind": "product",
            "owner": "sonnet", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-RES-1.md",
            "procedureSha256": sha256_of(proc)}
    try:
        result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-RES-1", dry_run=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SHIM: lane=sonnet card=TEST-EDIT-RES-1" in result.stdout, result.stdout
        rows = [r for r in read_reservation_rows(dual) if r["card"] == "TEST-EDIT-RES-1"]
        assert len(rows) == 2, rows
        assert rows[0]["state"] == "reserved", rows
        assert rows[1]["state"] == "charged", rows
        assert rows[0]["reservationId"] == rows[1]["reservationId"]
        # Attempt history is append-only: 'launching' before the child, 'launched' after it returned.
        run_dirs = glob.glob(str(tmp_path / ".claude-state" / "fleet-runs" / "ws-TEST-EDIT-RES-1-*"))
        assert len(run_dirs) == 1, run_dirs
        history = [json.loads(l) for l in (Path(run_dirs[0]) / "dispatch-attempts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert [h["outcome"] for h in history] == ["launching", "launched"], history
        assert history[1]["cause"] == "lane-returned" and history[1]["laneExitCode"] == 0, history
        latest = json.loads((Path(run_dirs[0]) / "dispatch-attempt.json").read_text(encoding="utf-8"))
        assert latest["outcome"] == "launched", latest
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-RES-1")


def test_a_real_dispatch_refuses_when_the_kill_switch_is_armed_immediately_before_start(tmp_path):
    """The kill switch is re-checked immediately before this lane starts, not only once at the
    top of the loop's own cycle -- a long cycle can dispatch several lanes, and the switch may be
    armed between the cycle's check and this particular start."""
    dual, head = editing_board(tmp_path)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-KILL-1")
    (dual / "WORKSTREAM-LOOP-DISABLED").write_text("armed for test\n", encoding="utf-8")
    item = {"id": "TEST-EDIT-KILL-1", "state": "queued", "track": "product", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-KILL-1.md",
            "procedureSha256": sha256_of(proc)}
    try:
        result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-KILL-1", dry_run=False)
        assert result.returncode == 6, result.stdout + result.stderr
        assert "REFUSED kill-switch-armed" in result.stdout
        assert "SHIM:" not in result.stdout
        assert not [r for r in read_reservation_rows(dual) if r["card"] == "TEST-EDIT-KILL-1"]
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-KILL-1")


def test_kill_switch_armed_mid_run_by_the_lane_itself_blocks_the_next_dispatch(tmp_path):
    """MINOR 3 (sol round-1 review): the test above pre-arms the switch from Python BEFORE either
    dispatch runs, which an early one-time check (e.g. only at the top of a cycle, not
    immediately before each lane's own start) could also satisfy. Here the fake lane shim arms
    the switch itself, from inside what a real lane's own process would be, so only a check that
    re-reads the file at THIS dispatch's own start -- not a cached or once-per-cycle value -- can
    catch it before the second, separate dispatch."""
    dual, head = editing_board(tmp_path, with_lane_shim=False)
    proc_a = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-MIDRUN-A")
    proc_b = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-MIDRUN-B")
    kill_switch = dual / "WORKSTREAM-LOOP-DISABLED"
    shim = dual / "Start-EditingLane.ps1"
    shim.write_text(
        "param([string]$Lane,[string]$PromptFile,[string]$WorkDir,[string]$Card,"
        "[string]$RunDir,[string]$ExtraReadDir,[int]$TimeoutSec)\n"
        "Set-Content -LiteralPath '%s' -Value 'armed mid-run by the lane'\n"
        "Write-Output ('SHIM: lane=' + $Lane + ' card=' + $Card + ' workDir=' + $WorkDir)\n"
        "exit 0\n" % kill_switch.as_posix(),
        encoding="ascii",
    )
    items = [
        {"id": "TEST-EDIT-MIDRUN-A", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1,
         "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-MIDRUN-A.md",
         "procedureSha256": sha256_of(proc_a)},
        {"id": "TEST-EDIT-MIDRUN-B", "state": "queued", "track": "product", "kind": "product",
         "owner": "sonnet", "priority": 1,
         "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-MIDRUN-B.md",
         "procedureSha256": sha256_of(proc_b)},
    ]
    try:
        first = run_editing_dispatch(tmp_path, items, "TEST-EDIT-MIDRUN-A", dry_run=False)
        assert first.returncode == 0, first.stdout + first.stderr
        assert "SHIM: lane=sonnet card=TEST-EDIT-MIDRUN-A" in first.stdout, first.stdout
        assert kill_switch.exists(), "the shim did not arm the switch it was given"

        second = run_editing_dispatch(tmp_path, items, "TEST-EDIT-MIDRUN-B", dry_run=False)
        assert second.returncode == 6, second.stdout + second.stderr
        assert "REFUSED kill-switch-armed" in second.stdout
        assert "SHIM: lane=sonnet card=TEST-EDIT-MIDRUN-B" not in second.stdout
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-MIDRUN-A")
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-MIDRUN-B")


def test_the_reservation_row_exists_before_the_lane_itself_finishes(tmp_path):
    """MINOR 3 (sol round-1 review): appending a 'reserved' row before start and a 'charged' row
    after would also pass a check that only inspects the file once the whole dispatcher process
    has already returned. Here the fake lane shim reads the reservation file FROM INSIDE its own
    run and asserts its 'reserved' row for THIS card is already on disk -- proving the row lands
    before the lane's own process even finishes, not merely before the dispatcher's return."""
    dual, head = editing_board(tmp_path, with_lane_shim=False)
    proc = write_fields_card(dual / "prompts" / "v2", "TEST-EDIT-RESORDER-1")
    reservations = dual / "receipts" / "dispatch-reservations.jsonl"
    shim = dual / "Start-EditingLane.ps1"
    shim.write_text(
        "param([string]$Lane,[string]$PromptFile,[string]$WorkDir,[string]$Card,"
        "[string]$RunDir,[string]$ExtraReadDir,[int]$TimeoutSec)\n"
        "$rows = @()\n"
        "if (Test-Path -LiteralPath '%s') {\n"
        "    $rows = @(Get-Content -LiteralPath '%s' | ForEach-Object { $_ | ConvertFrom-Json })\n"
        "}\n"
        "$mine = @($rows | Where-Object { $_.card -eq $Card -and $_.state -eq 'reserved' })\n"
        "if ($mine.Count -eq 0) { Write-Output 'SHIM: NO-RESERVED-ROW-YET'; exit 1 }\n"
        "Write-Output ('SHIM: lane=' + $Lane + ' card=' + $Card + ' sawReservedRow=' + $mine[0].reservationId)\n"
        "exit 0\n" % (reservations.as_posix(), reservations.as_posix()),
        encoding="ascii",
    )
    item = {"id": "TEST-EDIT-RESORDER-1", "state": "queued", "track": "product", "kind": "product",
            "owner": "sonnet", "priority": 1,
            "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-TEST-EDIT-RESORDER-1.md",
            "procedureSha256": sha256_of(proc)}
    try:
        result = run_editing_dispatch(tmp_path, [item], "TEST-EDIT-RESORDER-1", dry_run=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sawReservedRow=" in result.stdout, result.stdout
        assert "NO-RESERVED-ROW-YET" not in result.stdout
    finally:
        cleanup_lane_worktree(tmp_path, "TEST-EDIT-RESORDER-1")


# --- Invoke-Lane.ps1's own allowlist-required refusal, reachable only if something calls it
# directly instead of going through Start-EditingLane.ps1 -- which is exactly why deliverable 3
# requires EVERY editing dispatch to go through the wrapper.

def test_invoke_lane_refuses_allowedtools_all(tmp_path):
    cmd = (
        "try { & '%s' -Lane sonnet -Prompt 'x' -WorkDir '%s' -AllowEdits -AllowedTools 'ALL' } "
        "catch { $_.Exception.Message }" % (LANE_RUNNER.as_posix(), tmp_path.as_posix())
    )
    result = run_pwsh_command(cmd)
    assert "allowlist-required" in (result.stdout + result.stderr)


def test_invoke_lane_refuses_editing_with_allowedtools_entirely_absent(tmp_path):
    """MINOR 2 (sol round-1 review): the existing test above only covers -AllowedTools 'ALL'.
    The implementation's guard is `IsNullOrWhiteSpace($AllowedTools) -or $AllowedTools -eq 'ALL'`
    -- an `-or` with two independently reachable branches -- and the absent-argument branch
    (which binds $AllowedTools to its default '') had no test of its own until now."""
    cmd = (
        "try { & '%s' -Lane sonnet -Prompt 'x' -WorkDir '%s' -AllowEdits } "
        "catch { $_.Exception.Message }" % (LANE_RUNNER.as_posix(), tmp_path.as_posix())
    )
    result = run_pwsh_command(cmd)
    assert "allowlist-required" in (result.stdout + result.stderr)


# --- Compose-LanePrompt.ps1 / compose-lane-prompt-core.ps1: determinism, PR_STEP literals, ---
# field echo, unknown-field refusal (deliverable 6)

PR_STEP_LANE_CAN_OPEN_PR = (
    'gh pr create -R SuperDimensionalSpaceFortress/MLV-App --head {branch} --title "<card id>: <subject>" '
    '--body "<what, why, red run, green run>"; then print PR-OPENED: <number> as your last line.'
)
PR_STEP_OTHERWISE = (
    'Do NOT call gh. Print PUSHED: {branch} <head sha> as your last line; the dispatcher opens the PR.'
)


def compose(procedure_path, gh_capability, work_dir="C:\\mlvtmp\\lane-x", base_sha="a" * 40,
            run_dir=None, ts="20260101T000000Z"):
    run_dir = run_dir or (Path(procedure_path).parent / "run")
    args = [
        "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(COMPOSE_CLI), "-ProcedurePath", str(procedure_path),
        "-WorkDir", work_dir, "-BaseSha", base_sha, "-RunDir", str(run_dir), "-Ts", ts,
        "-GhCapability", gh_capability,
    ]
    return subprocess.run(args, text=True, capture_output=True)


def test_composition_is_deterministic_byte_for_byte(tmp_path):
    proc = write_fields_card(tmp_path, "DET-1")
    r1 = compose(proc, "lane-can-open-pr")
    r2 = compose(proc, "lane-can-open-pr")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert r1.stdout == r2.stdout


def test_composed_fields_prompt_carries_the_lane_can_open_pr_literal_exactly(tmp_path):
    proc = write_fields_card(tmp_path, "PR-STEP-A")
    result = compose(proc, "lane-can-open-pr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert PR_STEP_LANE_CAN_OPEN_PR.format(branch="product/PR-STEP-A") in result.stdout


def test_composed_fields_prompt_carries_the_otherwise_literal_exactly(tmp_path):
    proc = write_fields_card(tmp_path, "PR-STEP-B")
    result = compose(proc, "no-pr-capability")
    assert result.returncode == 0, result.stdout + result.stderr
    assert PR_STEP_OTHERWISE.format(branch="product/PR-STEP-B") in result.stdout


def test_composed_full_card_prompt_carries_the_lane_can_open_pr_literal_exactly(tmp_path):
    card = tmp_path / "card-PR-STEP-C.md"
    card.write_text("# CARD: PR-STEP-C\nSomething.\n{{PR_STEP}}\n", encoding="utf-8")
    result = compose(card, "lane-can-open-pr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert PR_STEP_LANE_CAN_OPEN_PR.format(branch="product/PR-STEP-C") in result.stdout


def test_composed_full_card_prompt_carries_the_otherwise_literal_exactly(tmp_path):
    card = tmp_path / "card-PR-STEP-D.md"
    card.write_text("# CARD: PR-STEP-D\nSomething.\n{{PR_STEP}}\n", encoding="utf-8")
    result = compose(card, "no-pr-capability")
    assert result.returncode == 0, result.stdout + result.stderr
    assert PR_STEP_OTHERWISE.format(branch="product/PR-STEP-D") in result.stdout


def test_every_parsed_field_appears_byte_for_byte_in_the_composed_prompt(tmp_path):
    proc = write_fields_card(tmp_path, "FIELD-ECHO-1")
    text = proc.read_text(encoding="ascii")
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*):\s?(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2)
    result = compose(proc, "lane-can-open-pr")
    assert result.returncode == 0, result.stdout + result.stderr
    for label, value in fields.items():
        if label == "CARD_ID":
            continue  # substituted in multiple places; covered implicitly by the branch/title
        assert value in result.stdout, "field %s=%r missing byte-for-byte from composed prompt" % (label, value)


def test_compose_cli_refuses_an_unknown_field(tmp_path):
    proc = write_fields_card(tmp_path, "UNK-CLI-1", extra_lines="ZORP: not a real field\n")
    result = compose(proc, "lane-can-open-pr")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.startswith("REFUSED: unknown-field")


# --- Export-PrReviewEvidence.ps1: pinned repo, byte-exact exports, drift refusal, missing --
# required context reported as a failure (deliverable 9)

FAKE_GH_REPO_STRING = "SuperDimensionalSpaceFortress/MLV-App"
# The immutable GitHub repository id the exporter pins (gh api repos/<slug> --jq .id).
FAKE_GH_REPO_ID = "1169162800"

FAKE_GH_SCRIPT = (
    "$stateDir = $env:FAKE_GH_STATE_DIR\n"
    "Add-Content -LiteralPath (Join-Path $stateDir 'call_log.txt') -Value ($args -join '|')\n"
    "function Get-NextLine([string]$Path, [string]$CounterPath) {\n"
    "    $n = 0\n"
    "    if (Test-Path -LiteralPath $CounterPath) { $n = [int](Get-Content -LiteralPath $CounterPath -Raw) }\n"
    "    $n = $n + 1\n"
    "    Set-Content -LiteralPath $CounterPath -Value $n\n"
    "    $lines = @(Get-Content -LiteralPath $Path)\n"
    "    return $lines[$n - 1]\n"
    "}\n"
    "if ($args.Count -ge 2 -and $args[0] -eq 'api' -and $args[1] -eq 'repos/" + FAKE_GH_REPO_STRING + "') {\n"
    "    if ($env:FAKE_GH_REPO_ID -eq 'unreadable') { exit 1 }\n"
    "    if ($env:FAKE_GH_REPO_ID) { Write-Output $env:FAKE_GH_REPO_ID } else { Write-Output '" + FAKE_GH_REPO_ID + "' }\n"
    "    exit 0\n"
    "}\n"
    "if ($args.Count -ge 2 -and $args[0] -eq 'pr' -and $args[1] -eq 'view') {\n"
    "    Write-Output (Get-NextLine (Join-Path $stateDir 'pr_view.jsonl') (Join-Path $stateDir 'pr_view.count'))\n"
    "    exit 0\n"
    "}\n"
    "if ($args.Count -ge 1 -and $args[0] -eq 'api' -and (($args -join ' ') -match 'branches/master/protection')) {\n"
    "    Write-Output (Get-NextLine (Join-Path $stateDir 'protection.jsonl') (Join-Path $stateDir 'protection.count'))\n"
    "    exit 0\n"
    "}\n"
    "if ($args.Count -ge 2 -and $args[0] -eq 'pr' -and $args[1] -eq 'checks') {\n"
    "    Get-Content -LiteralPath (Join-Path $stateDir 'checks.json') -Raw\n"
    "    exit 0\n"
    "}\n"
    "Write-Error ('fake-gh: unrecognized args: ' + ($args -join ' '))\n"
    "exit 1\n"
)


def make_fake_gh(dir_path, pr_view_sequence, protection_sequence, checks_payload):
    state = dir_path / "fake-gh-state"
    state.mkdir()
    (state / "pr_view.jsonl").write_text("\n".join(json.dumps(x) for x in pr_view_sequence), encoding="utf-8")
    (state / "protection.jsonl").write_text("\n".join(json.dumps(x) for x in protection_sequence), encoding="utf-8")
    (state / "checks.json").write_text(json.dumps(checks_payload), encoding="utf-8")
    (state / "call_log.txt").write_text("", encoding="utf-8")
    shim = dir_path / "fake-gh.ps1"
    shim.write_text(FAKE_GH_SCRIPT, encoding="ascii")
    return shim, state


def pr_evidence_repo(tmp_path, with_fork_remote=True):
    """A repo with base/head commits and (by default) a REAL `fork` remote pinned at the base
    commit, so a genuine `git fetch fork` (MAJOR 2, sol round-1 review) succeeds instead of
    silently no-op'ing against a remote that was never more than a manually poked ref.

    The remote is a bare snapshot taken right after the base commit, not an alias to `repo`
    itself: `repo`'s own branch keeps moving (the head commit below), and if `fork` pointed at
    that same path, fetching AFTER the head commit would walk the live branch forward and
    silently repoint `fork/master` at head_sha instead of base_sha.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    # Named explicitly, not left to `init.defaultBranch`: the exporter hardcodes `fork/master`,
    # so the fork remote's default branch must actually be called `master` for a real fetch to
    # populate `refs/remotes/fork/master`.
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = git(repo, "rev-parse", "HEAD")

    if with_fork_remote:
        fork_remote = tmp_path / "fork_remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(fork_remote)], check=True)
        subprocess.run(["git", "remote", "add", "fork", str(fork_remote)], cwd=repo, check=True)
    # else: no remote named 'fork' at all -- `git fetch fork` fails deterministically (exit 128,
    # "'fork' does not appear to be a git repository"), the fetch-failure case MAJOR 2 covers.

    (repo / "f.txt").write_text("head\n")
    subprocess.run(["git", "commit", "-qam", "head commit"], cwd=repo, check=True)
    head_sha = git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def run_exporter(repo, run_dir, gh_shim, state_dir, pr_number=99, repo_id=None):
    env = dict(os.environ)
    env["FAKE_GH_STATE_DIR"] = str(state_dir)
    if repo_id is not None:
        env["FAKE_GH_REPO_ID"] = repo_id
    args = [
        "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(EXPORT_PR_EVIDENCE), "-PrNumber", str(pr_number), "-RunDir", str(run_dir),
        "-RepoRoot", str(repo), "-GhExe", str(gh_shim),
    ]
    return subprocess.run(args, text=True, capture_output=True, env=env)


def test_exporter_pins_the_repository_on_every_gh_call(tmp_path):
    """MAJOR 3 (sol round-1 review): the old assertion only checked that the repository STRING
    occurred SOMEWHERE in the joined argv line, which a repo name appearing in the wrong place
    (or coincidentally inside the API endpoint's path) would also satisfy -- it could false-green
    a `-R` flag that carried the wrong value, or a `gh api` call with no repo pin at all. This
    checks the actual argv TOKEN immediately after `-R` on every `pr view`/`pr checks` call, and
    the exact `repos/<repo>/branches/master/protection` endpoint string on the `gh api` call --
    not a substring hit against the whole line."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
        ],
        protection_sequence=[["build"], ["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (state / "call_log.txt").read_text(encoding="utf-8").splitlines()
    assert calls, "gh was never invoked"
    saw_pr_call = False
    saw_api_call = False
    saw_id_call = False
    for line in calls:
        tokens = line.split("|")
        if tokens[:2] in (["pr", "view"], ["pr", "checks"]):
            saw_pr_call = True
            assert "-R" in tokens, "no -R flag on a pr view/checks call: %s" % line
            idx = tokens.index("-R")
            assert tokens[idx + 1] == FAKE_GH_REPO_STRING, (
                "the -R flag does not carry the exact pinned repo (got %r): %s" % (tokens[idx + 1], line)
            )
        if tokens[0] == "api" and tokens[1] == "repos/%s" % FAKE_GH_REPO_STRING:
            saw_id_call = True
            assert tokens[2:] == ["--jq", ".id"], "the repository id call carries unexpected args: %s" % line
            continue
        if tokens[0] == "api":
            saw_api_call = True
            endpoint = tokens[1]
            assert endpoint == "repos/%s/branches/master/protection" % FAKE_GH_REPO_STRING, (
                "the gh api endpoint is not the exact pinned repo path: %s" % endpoint
            )
    assert saw_pr_call, "no pr view/checks call was observed"
    assert saw_api_call, "no gh api call was observed"
    assert saw_id_call, "the immutable repository id was never read from the API"


def test_exporter_repo_is_a_hardcoded_pin_not_a_caller_supplied_parameter(tmp_path):
    """MAJOR 3 (sol round-1 review): '-Repo is caller-overridable' was the actual defect behind
    the weak test above -- a parameter with a safe-looking default is still an argv path that can
    carry a different value in. The fix removes the parameter entirely rather than merely
    validating it, so this asserts on the source that no such parameter exists."""
    text = EXPORT_PR_EVIDENCE.read_text(encoding="utf-8")
    # Scoped to the SCRIPT's own param() block, not the internal Get-PrView/Get-RequiredContexts
    # helper functions further down, which legitimately take a $Repo parameter fed from the one
    # pinned script-scope value below -- that is an implementation detail, not a caller-facing
    # argv path. \b so this also does not false-positive on the unrelated [string]$RepoRoot.
    script_param_block = text[text.index("[CmdletBinding()]"):text.index("$ErrorActionPreference")]
    assert not re.search(r"\[string\]\$Repo\b", script_param_block), (
        "the repository is still a caller-settable parameter: %r" % script_param_block
    )
    assert "'%s'" % FAKE_GH_REPO_STRING in text, "the pinned repo is no longer a literal in the source"
    assert "$RepoId = %s" % FAKE_GH_REPO_ID in text, "the pinned repository id is no longer a literal in the source"


def test_exporter_refuses_when_the_repository_id_differs(tmp_path):
    """ORG-MOVE-TRACKED-SLUG-RESIDUE-1: the pin is the immutable repository id. A slug that now
    resolves to a DIFFERENT repository (re-created after a transfer) must refuse closed, before
    any evidence is written and before the first `pr view`."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[{"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"}] * 2,
        protection_sequence=[["build"], ["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state, repo_id="42")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "REFUSED: repo-id-mismatch" in result.stdout
    assert "expected=%s actual=42" % FAKE_GH_REPO_ID in result.stdout
    assert not list(run_dir.glob("pr-*")), "evidence was written for a repository that failed the id pin"
    calls = (state / "call_log.txt").read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("pr|") for line in calls), "a PR call ran before the id pin passed: %r" % calls


def test_exporter_refuses_when_the_repository_id_is_unreadable(tmp_path):
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[{"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"}] * 2,
        protection_sequence=[["build"], ["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state, repo_id="unreadable")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "REFUSED: repo-id-unreadable" in result.stdout
    assert not list(run_dir.glob("pr-*"))


def test_repo_slug_literals_agree_across_tools_coordination():
    """repo-slug.ps1 is the one shared definition; the two callers that keep their own literal
    (see repo-slug.ps1 for why) must carry exactly that value."""
    shared = re.search(r"return '([^']+/[^']+)'", (ROOT / "tools" / "coordination" / "repo-slug.ps1").read_text(encoding="utf-8"))
    assert shared, "repo-slug.ps1 no longer returns a slug literal"
    assert shared.group(1) == FAKE_GH_REPO_STRING
    # The EFFECTIVE literal each script talks to, read from its assigning statement (a comment or
    # SYNOPSIS line naming the slug proves nothing: the endpoint itself is what gets called).
    effective = {
        "Export-PrReviewEvidence.ps1": r"^\$Repo = '([^']+)'\s*$",
        "set-required-checks.ps1": r"^\$endpoint = 'repos/([^']+)/branches/master/protection/required_status_checks'\s*$",
    }
    for name, pattern in effective.items():
        text = (ROOT / "tools" / "coordination" / name).read_text(encoding="utf-8")
        found = re.findall(pattern, text, re.M)
        assert found == [shared.group(1)], "%s effective repository literal %r != shared slug" % (name, found)


def test_lane_prompt_template_contract_names_the_shared_slug():
    """product-card-TEMPLATE.md is the text every implementer prompt is composed from, and its
    COMPOSER CONTRACT is where compose-lane-prompt-core.ps1 says the PR_STEP literal comes from
    verbatim: the contract, the remote line and the composer must all carry the shared slug."""
    shared = re.search(r"return '([^']+/[^']+)'", (ROOT / "tools" / "coordination" / "repo-slug.ps1").read_text(encoding="utf-8")).group(1)
    template = (ROOT / "docs" / "lane-prompts" / "v2" / "product-card-TEMPLATE.md").read_text(encoding="utf-8")
    assert "gh pr create -R %s --head {{BRANCH}}" % shared in template
    assert "Remote `fork` = `https://github.com/%s.git`" % shared in template
    hub = (ROOT / "docs" / "lane-prompts" / "v2" / "hub-procedure.md").read_text(encoding="utf-8")
    assert "`-R %s`" % shared in hub and "repos/%s/" % shared in hub
    composer = (ROOT / "tools" / "coordination" / "compose-lane-prompt-core.ps1").read_text(encoding="utf-8")
    assert "'gh pr create -R ' + (Get-MlvRepoSlug) + ' --head {{BRANCH}}" in composer


def extract_json_array_bytes(raw, key):
    """Slice out the raw bytes of a top-level JSON array value for `key` (e.g. b'"checks":
    [...]'), by bracket-balancing rather than reparsing -- so the comparison below is a real
    byte comparison of what was WRITTEN, not a reparse-and-recompare of what was MEANT."""
    marker = ('"%s":' % key).encode("ascii")
    idx = raw.index(marker)
    start = raw.index(b"[", idx)
    depth = 0
    i = start
    while i < len(raw):
        c = raw[i:i + 1]
        if c == b"[":
            depth += 1
        elif c == b"]":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
        i += 1
    raise AssertionError("unbalanced [ ] while extracting %r from JSON" % key)


def test_exporter_writes_both_exports_byte_exact(tmp_path):
    """MINOR 1 (sol round-1 review): a test named 'byte exact' that only reparses both files with
    json.loads and compares fields never actually compares a single byte -- BOM, whitespace,
    key ordering or any other byte-level change would still pass. The exporter writes the SAME
    `checks` array into both pr-99-checks.json and pr-99-review.json from the same $checks value,
    so their serialized `checks` bytes must be IDENTICAL; that is asserted here as a real
    raw-bytes comparison, on top of (not instead of) the existing semantic checks."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    checks_payload = [{"name": "build", "state": "SUCCESS", "link": "x"}]
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[
            {"number": 99, "headRefOid": head_sha, "body": "the body", "state": "OPEN"},
            {"number": 99, "headRefOid": head_sha, "body": "the body", "state": "OPEN"},
        ],
        protection_sequence=[["build"], ["build"]],
        checks_payload=checks_payload,
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode == 0, result.stdout + result.stderr
    checks_raw = (run_dir / "pr-99-checks.json").read_bytes()
    review_raw = (run_dir / "pr-99-review.json").read_bytes()

    # the actual byte comparison the test's name claims
    checks_bytes = extract_json_array_bytes(checks_raw, "checks")
    review_checks_bytes = extract_json_array_bytes(review_raw, "checks")
    assert checks_bytes == review_checks_bytes, (
        "the 'checks' array is not byte-identical between pr-99-checks.json and "
        "pr-99-review.json: %r != %r" % (checks_bytes, review_checks_bytes)
    )

    checks_doc = json.loads(checks_raw.decode("utf-8"))
    review_doc = json.loads(review_raw.decode("utf-8"))
    assert checks_doc["checks"] == checks_payload
    assert review_doc["number"] == 99
    assert review_doc["stateBefore"] == "OPEN" and review_doc["stateAfter"] == "OPEN"
    assert review_doc["headRefOidBefore"] == head_sha
    assert review_doc["headRefOidAfter"] == head_sha
    assert review_doc["requiredContextsBefore"] == ["build"]
    assert review_doc["requiredContextsAfter"] == ["build"]
    assert review_doc["body"] == "the body"
    assert review_doc["checks"] == checks_payload
    assert review_doc["missingRequiredContexts"] == []


def test_exporter_refuses_when_git_fetch_fails(tmp_path):
    """MAJOR 2 (sol round-1 review): `git fetch fork` used to be piped to Out-Null with its exit
    code never checked, so a fetch failure was indistinguishable from success and the exporter
    proceeded to review whatever objects happened to already be local. `with_fork_remote=False`
    means no remote named 'fork' exists at all, so the fetch fails deterministically."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path, with_fork_remote=False)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[{"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"}],
        protection_sequence=[["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode != 0
    assert "REFUSED: git-fetch-failed" in result.stdout, result.stdout + result.stderr
    assert not (state / "call_log.txt").read_text(encoding="utf-8").strip(), (
        "gh must never be called after a failed fetch"
    )
    assert not run_dir.exists() or not any(run_dir.iterdir())


def test_exporter_refuses_on_an_empty_head_sha(tmp_path):
    """MAJOR 2 (sol round-1 review): the old `if ($sha) { cat-file -e ... }` SKIPPED the
    commit-existence check entirely for a falsy value, so an empty headRefOid silently passed
    with no object ever verified. An empty or short sha must be a REFUSAL, not a skipped check."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[{"number": 99, "headRefOid": "", "body": "b", "state": "OPEN"}],
        protection_sequence=[["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode != 0
    assert "REFUSED: pr-sha-invalid field=head" in result.stdout, result.stdout + result.stderr
    assert not (run_dir / "pr-99-checks.json").exists()
    assert not (run_dir / "pr-99-review.json").exists()


def test_exporter_refuses_on_a_non_40_hex_head_sha(tmp_path):
    """MAJOR 2 (sol round-1 review): a short or otherwise malformed value is just as unbindable
    as an empty one -- both must fail the same explicit shape check before any cat-file call."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[{"number": 99, "headRefOid": "not-a-sha", "body": "b", "state": "OPEN"}],
        protection_sequence=[["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode != 0
    assert "REFUSED: pr-sha-invalid field=head value=not-a-sha" in result.stdout, result.stdout + result.stderr


def test_exporter_refuses_on_head_drift(tmp_path):
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
            {"number": 99, "headRefOid": "f" * 40, "body": "b", "state": "OPEN"},
        ],
        protection_sequence=[["build"], ["build"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode != 0
    assert "REFUSED: pr-head-drift" in result.stdout
    assert not (run_dir / "pr-99-checks.json").exists()
    assert not (run_dir / "pr-99-review.json").exists()


def test_exporter_refuses_on_required_context_drift(tmp_path):
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
        ],
        protection_sequence=[["build"], ["build", "extra-check"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode != 0
    assert "REFUSED: required-context-drift" in result.stdout


def test_exporter_reports_a_missing_required_context_as_a_failure(tmp_path):
    """'lint' is required but never ran (absent from checks entirely, not merely non-SUCCESS)
    -- it must be reported, never silently folded into either verdict."""
    repo, base_sha, head_sha = pr_evidence_repo(tmp_path)
    run_dir = tmp_path / "run"
    shim, state = make_fake_gh(
        tmp_path,
        pr_view_sequence=[
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
            {"number": 99, "headRefOid": head_sha, "body": "b", "state": "OPEN"},
        ],
        protection_sequence=[["build", "lint"], ["build", "lint"]],
        checks_payload=[{"name": "build", "state": "SUCCESS", "link": "x"}],
    )
    result = run_exporter(repo, run_dir, shim, state)
    assert result.returncode == 0, result.stdout + result.stderr
    review_doc = json.loads((run_dir / "pr-99-review.json").read_text(encoding="utf-8"))
    assert review_doc["missingRequiredContexts"] == ["lint"]
    assert "EXPORT: missing-required-context lint" in result.stdout


# Product ratio admission: disposable history and actual dispatcher fixtures.
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RATIO_SOURCE_GUARD = ROOT / "tools" / "coordination" / "Test-ProductRatioGuard.ps1"
RATIO_SOURCE_DISPATCHER = ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1"
RATIO_SOURCE_AS_OF = 2_000_000_000
RATIO_SOURCE_DAY = 86400


def ratio_source_run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)


def ratio_source_git(repo, *args, env=None):
    result = ratio_source_run("git", *args, cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def ratio_source_init_repo(path):
    path.mkdir()
    ratio_source_git(path, "init", "-q", "-b", "master")
    ratio_source_git(path, "config", "user.name", "Ratio Test")
    ratio_source_git(path, "config", "user.email", "ratio@example.invalid")
    return path


def ratio_source_commit(repo, subject, epoch, paths, author_epoch=None):
    for name, text in paths.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    ratio_source_git(repo, "add", "-A")
    env = dict(os.environ)
    env["GIT_COMMITTER_DATE"] = f"@{epoch} +0000"
    env["GIT_AUTHOR_DATE"] = f"@{author_epoch if author_epoch is not None else epoch} +0000"
    ratio_source_git(repo, "commit", "-qm", subject, env=env)
    return ratio_source_git(repo, "rev-parse", "HEAD")


def ratio_source_invoke_guard(repo, *, as_of=RATIO_SOURCE_AS_OF, reservations=None, legacy=None, ref="master"):
    command = [
        "pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(RATIO_SOURCE_GUARD), "-RepoRoot", str(repo), "-SourceRef", ref,
        "-AsOfEpoch", str(as_of),
    ]
    if reservations is not None:
        command += ["-ReservationsPath", str(reservations)]
    if legacy is not None:
        command += ["-LegacyDispatchPath", str(legacy)]
    result = ratio_source_run(*command)
    payload = json.loads(result.stdout)
    return result, payload


def ratio_source_populate(repo, count, product_indices, *, start=RATIO_SOURCE_AS_OF - RATIO_SOURCE_DAY):
    for index in range(count):
        path = f"src/p{index}.txt" if index in product_indices else f"docs/d{index}.txt"
        ratio_source_commit(repo, f"commit {index}", start + index, {path: str(index)})


@pytest.mark.parametrize("count,products,expected", [(10, set(range(6)), 0.60), (20, {3}, 0.05)])
def test_ratio_source_product_share_populations(tmp_path, count, products, expected):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_populate(repo, count, products)
    _, payload = ratio_source_invoke_guard(repo)
    assert payload["commitPopulation"] == count
    assert payload["productCommitCount"] == len(products)
    assert payload["productShare7d"] == pytest.approx(expected)
    assert payload["verdict"] == "RED"


def test_ratio_source_boundary_committer_date_docs_merge_and_mixed_commit(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "included boundary", RATIO_SOURCE_AS_OF - 7 * RATIO_SOURCE_DAY, {"src/a": "a"}, author_epoch=RATIO_SOURCE_AS_OF - 30 * RATIO_SOURCE_DAY)
    ratio_source_commit(repo, "excluded old", RATIO_SOURCE_AS_OF - 7 * RATIO_SOURCE_DAY - 1, {"src/old": "old"}, author_epoch=RATIO_SOURCE_AS_OF)
    ratio_source_commit(repo, "docs denominator", RATIO_SOURCE_AS_OF - 10, {"docs/readme": "d"})
    ratio_source_commit(repo, "mixed once", RATIO_SOURCE_AS_OF - 9, {"src/m": "m", "docs/m": "m"})
    ratio_source_git(repo, "checkout", "-qb", "side", "HEAD~1")
    ratio_source_commit(repo, "side", RATIO_SOURCE_AS_OF - 8, {"platform/side": "s"})
    ratio_source_git(repo, "checkout", "-q", "master")
    env = dict(os.environ, GIT_COMMITTER_DATE=f"@{RATIO_SOURCE_AS_OF - 7} +0000", GIT_AUTHOR_DATE=f"@{RATIO_SOURCE_AS_OF - 7} +0000")
    ratio_source_git(repo, "merge", "--no-ff", "-qm", "ordinary merge", "side", env=env)
    _, payload = ratio_source_invoke_guard(repo)
    assert payload["commitPopulation"] == 4
    assert payload["productCommitCount"] == 3


def test_ratio_source_future_commit_is_excluded(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "present", RATIO_SOURCE_AS_OF, {"src/a": "a"})
    ratio_source_commit(repo, "future", RATIO_SOURCE_AS_OF + 1, {"src/b": "b"})
    _, payload = ratio_source_invoke_guard(repo)
    assert payload["commitPopulation"] == 1
    assert payload["productCommitCount"] == 1


def test_ratio_source_github_merge_squash_dedup_and_batch_count_once(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "base", RATIO_SOURCE_AS_OF - 100, {"docs/base": "base"})
    ratio_source_git(repo, "checkout", "-qb", "feature")
    ratio_source_commit(repo, "part one", RATIO_SOURCE_AS_OF - 90, {"src/a": "a"})
    ratio_source_commit(repo, "part two", RATIO_SOURCE_AS_OF - 80, {"src/b": "b"})
    ratio_source_git(repo, "checkout", "-q", "master")
    env = dict(os.environ, GIT_COMMITTER_DATE=f"@{RATIO_SOURCE_AS_OF - 70} +0000", GIT_AUTHOR_DATE=f"@{RATIO_SOURCE_AS_OF - 70} +0000")
    ratio_source_git(repo, "merge", "--no-ff", "-qm", "Merge pull request #91 from example/batch", "feature", env=env)
    ratio_source_commit(repo, "squashed product (#92)", RATIO_SOURCE_AS_OF - 60, {"platform/c": "c"})
    _, payload = ratio_source_invoke_guard(repo)
    assert payload["recognizedProductPrIds"] == [91, 92]
    assert payload["recognizedProductPrCount"] == 2


def test_ratio_source_unknown_product_landing_nulls_rate(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "direct product landing", RATIO_SOURCE_AS_OF - 2, {"src/a": "a"})
    reservations = tmp_path / "reservations.jsonl"
    reservations.write_text(json.dumps({"state": "reserved", "recordedUtc": datetime.fromtimestamp(RATIO_SOURCE_AS_OF - 1, timezone.utc).isoformat()}) + "\n")
    _, payload = ratio_source_invoke_guard(repo, reservations=reservations)
    assert payload["unrecognizedProductLandings"]
    assert payload["dispatchesPerLandedProductPr7dLowerBound"] is None
    assert "UNAVAILABLE_LANDING_PROVENANCE" in payload["reasons"]


def test_ratio_source_missing_and_malformed_observations_are_partial_red(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "docs", RATIO_SOURCE_AS_OF - 1, {"docs/a": "a"})
    missing = tmp_path / "missing"
    _, absent = ratio_source_invoke_guard(repo, reservations=missing, legacy=missing)
    assert absent["dispatchCoverage"] == "PARTIAL"
    assert absent["dispatchesObserved"] == 0
    assert absent["verdict"] == "RED"
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not-json\n")
    _, malformed = ratio_source_invoke_guard(repo, reservations=bad, legacy=missing)
    assert malformed["malformedDispatchRows"] == 1
    assert malformed["verdict"] == "RED"
    assert "ERROR_DISPATCH_EVIDENCE_MALFORMED" in malformed["reasons"]


def test_ratio_source_reservations_take_precedence_and_are_not_combined_with_legacy(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "docs", RATIO_SOURCE_AS_OF - 1, {"docs/a": "a"})
    stamp = datetime.fromtimestamp(RATIO_SOURCE_AS_OF - 1, timezone.utc).isoformat()
    reservations = tmp_path / "reservations.jsonl"
    legacy = tmp_path / "legacy.jsonl"
    reservations.write_text(json.dumps({"state": "reserved", "recordedUtc": stamp}) + "\n")
    legacy.write_text("\n".join(json.dumps({"dispatchedUtc": stamp}) for _ in range(4)) + "\n")
    _, payload = ratio_source_invoke_guard(repo, reservations=reservations, legacy=legacy)
    assert payload["dispatchEvidenceSource"] == "dispatch-reservations"
    assert payload["dispatchesObserved"] == 1


def test_ratio_source_invalid_ref_and_invalid_repo_are_error_exit_3(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "base", RATIO_SOURCE_AS_OF - 1, {"docs/a": "a"})
    result, payload = ratio_source_invoke_guard(repo, ref="missing")
    assert result.returncode == 3
    assert payload["verdict"] == "ERROR"
    assert payload["errorCode"] == "ERROR_REF_UNRESOLVED"
    invalid = tmp_path / "not-repo"
    invalid.mkdir()
    result, payload = ratio_source_invoke_guard(invalid)
    assert result.returncode == 3
    assert payload["verdict"] == "ERROR"


def ratio_source_extract_decision_function(tmp_path):
    source = RATIO_SOURCE_GUARD.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^function New-Decision \{.*?^\}", source)
    assert match
    script = tmp_path / "decision.ps1"
    script.write_text("param([double]$Rate)\n" + match.group(0) + "\nNew-Decision -ProductShare 0.6 -Coverage COMPLETE -EvidenceAvailable $true -ProvenanceComplete $true -HasProductLandings $true -DispatchRate $Rate | ConvertTo-Json -Compress\n", encoding="utf-8")
    return script


def test_ratio_source_synthetic_complete_decision_green_at_four_and_red_above(tmp_path):
    script = ratio_source_extract_decision_function(tmp_path)
    green = ratio_source_run("pwsh", "-NoProfile", "-File", str(script), "-Rate", "4")
    red = ratio_source_run("pwsh", "-NoProfile", "-File", str(script), "-Rate", "4.01")
    assert json.loads(green.stdout)["verdict"] == "GREEN"
    assert json.loads(red.stdout)["verdict"] == "RED"
    assert "RED_DISPATCH_RATE" in json.loads(red.stdout)["reasons"]


def test_ratio_source_caller_has_helper_at_both_post_kill_pre_reservation_seams():
    source = RATIO_SOURCE_DISPATCHER.read_text(encoding="utf-8")
    assert source.count("function Test-RatioDispatchPermission") == 1
    assert source.count("$ratioExit = Test-RatioDispatchPermission -Kind $cardKind") == 2
    for match in re.finditer(r"\$ratioExit = Test-RatioDispatchPermission -Kind \$cardKind", source):
        prefix = source[:match.start()]
        suffix = source[match.end():]
        assert prefix.rfind("if (Test-KillSwitchArmed)") > prefix.rfind("if ($DryRun)")
        assert suffix.find("Write-DispatchReservation") >= 0
        assert suffix.find("Write-DispatchReservation") < suffix.find("& pwsh")


def ratio_source_make_fake_guard(path, verdict, reasons=None, rate=None, malformed=False):
    rate = 4 if rate is None else rate
    payload = ratio_guard_payload(
        dispatchesPerLandedProductPr7dLowerBound=rate, dispatchesObserved=int(rate),
        dispatchCoverage="COMPLETE" if verdict == "GREEN" else "PARTIAL",
        verdict=verdict, reasons=reasons or [],
        errorCode="ERROR_GIT_HISTORY" if verdict == "ERROR" else None,
    )
    body = "Write-Output 'not-json'" if malformed else "Write-Output '" + json.dumps(payload, separators=(",", ":")) + "'"
    path.write_text(body + "\nexit " + ("3" if verdict == "ERROR" else "0") + "\n", encoding="utf-8")


def test_ratio_source_caller_helper_routing_and_malformed_output(tmp_path):
    source = RATIO_SOURCE_DISPATCHER.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^function Test-RatioDispatchPermission \{.*?^\}", source)
    assert match
    harness = tmp_path / "harness.ps1"
    guard = tmp_path / "guard.ps1"
    harness.write_text("param($Guard,$Kind)\n$ProductRatioGuard=$Guard\n$RepoRoot='x'\n" + match.group(0) + "\nexit (Test-RatioDispatchPermission -Kind $Kind)\n", encoding="utf-8")
    for kind, expected in [("product", 0), ("playback", 0), ("factory", 6), ("", 6)]:
        ratio_source_make_fake_guard(guard, "RED", ["RED_DISPATCH_COVERAGE_PARTIAL"])
        result = ratio_source_run("pwsh", "-NoProfile", "-File", str(harness), "-Guard", str(guard), "-Kind", kind)
        assert result.returncode == expected
    ratio_source_make_fake_guard(guard, "ERROR", ["ERROR_GIT_HISTORY"])
    assert ratio_source_run("pwsh", "-NoProfile", "-File", str(harness), "-Guard", str(guard), "-Kind", "product").returncode == 3
    ratio_source_make_fake_guard(guard, "GREEN")
    assert ratio_source_run("pwsh", "-NoProfile", "-File", str(harness), "-Guard", str(guard), "-Kind", "factory").returncode == 0
    ratio_source_make_fake_guard(guard, "RED", malformed=True)
    assert ratio_source_run("pwsh", "-NoProfile", "-File", str(harness), "-Guard", str(guard), "-Kind", "product").returncode == 3


def test_ratio_source_dry_run_precedes_reservation_and_launch_in_both_paths():
    source = RATIO_SOURCE_DISPATCHER.read_text(encoding="utf-8")
    read_only = source[source.index("if (-not $AllowEdits)"):source.index("# ==================================================================== editing dispatch")]
    editing = source[source.index("# ==================================================================== editing dispatch"):]
    assert read_only.index("if ($DryRun)") < read_only.index("Write-DispatchReservation") < read_only.index("& pwsh")
    assert editing.index("if ($DryRun)") < editing.index("Write-DispatchReservation") < editing.index("& pwsh")


def test_ratio_source_dispatch_timestamp_boundaries_preserve_explicit_offsets(tmp_path):
    repo=ratio_source_init_repo(tmp_path/'repo')
    ratio_source_commit(repo,'base',RATIO_SOURCE_AS_OF-2,{'docs/base':'base'})
    records=[]
    for epoch in [RATIO_SOURCE_AS_OF-7*RATIO_SOURCE_DAY-1,RATIO_SOURCE_AS_OF-7*RATIO_SOURCE_DAY,RATIO_SOURCE_AS_OF,RATIO_SOURCE_AS_OF+1]:
        records.append({'state':'reserved','recordedUtc':datetime.fromtimestamp(epoch,timezone.utc).isoformat()})
    path=tmp_path/'reservations.jsonl'
    path.write_text('\n'.join(json.dumps(x) for x in records)+'\n')
    result,payload=ratio_source_invoke_guard(repo,reservations=path)
    assert result.returncode==0,result.stderr
    assert payload['dispatchesObserved']==2 and payload['malformedDispatchRows']==0


def test_ratio_source_old_history_does_not_spawn_a_process_per_commit(tmp_path):
    repo=ratio_source_init_repo(tmp_path/'repo')
    ratio_source_populate(repo,30,set(),start=RATIO_SOURCE_AS_OF-30*RATIO_SOURCE_DAY)
    ratio_source_commit(repo,'current',RATIO_SOURCE_AS_OF,{'src/current':'current'})
    trace=tmp_path/'git-trace.jsonl'
    env=dict(os.environ,GIT_TRACE2_EVENT=str(trace))
    result=ratio_source_run('pwsh','-NoProfile','-File',str(RATIO_SOURCE_GUARD),'-RepoRoot',str(repo),'-SourceRef','master','-AsOfEpoch',str(RATIO_SOURCE_AS_OF),env=env)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['commitPopulation']==1
    starts=[json.loads(line) for line in trace.read_text().splitlines() if json.loads(line).get('event')=='start']
    assert len(starts)<=8,[(x.get('argv')) for x in starts]



# Prefix every addition to avoid collisions with the existing guardrail helpers.
RATIO_GUARD = ROOT / "tools" / "coordination" / "Test-ProductRatioGuard.ps1"
RATIO_WORKSTREAM = ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1"
RATIO_LOOP = ROOT / "tools" / "coordination" / "Invoke-WorkstreamLoop.ps1"


def ratio_guard_payload(**overrides):
    payload = {
        "schema": "mlv-app/product-ratio-guard/v1",
        "asOfUtc": "2033-05-18T03:33:20.0000000Z",
        "windowStartUtc": "2033-05-11T03:33:20.0000000Z",
        "windowEndUtc": "2033-05-18T03:33:20.0000000Z",
        "sourceRef": "fork/master",
        "sourceSha": "1" * 40,
        "commitPopulation": 10,
        "productCommitCount": 6,
        "productShare7d": 0.6,
        "productShareThreshold": 0.5,
        "recognizedProductPrCount": 1,
        "recognizedProductPrIds": [101],
        "unrecognizedProductLandings": [],
        "landingProvenanceComplete": True,
        "hasProductLandings": True,
        "dispatchEvidenceSource": "dispatch-reservations",
        "dispatchCoverage": "PARTIAL",
        "dispatchEvidenceAvailable": True,
        "dispatchesObserved": 5,
        "malformedDispatchRows": 0,
        "dispatchesPerLandedProductPr7dLowerBound": 5.0,
        "dispatchRateThreshold": 4.0,
        "verdict": "RED",
        "reasons": ["RED_DISPATCH_COVERAGE_PARTIAL", "RED_DISPATCH_RATE"],
        "errorCode": None,
    }
    payload.update(overrides)
    return payload


def ratio_write_guard(path, payload, exit_code=0, raw=None):
    text = raw if raw is not None else json.dumps(payload, separators=(",", ":"))
    path.write_text("Write-Output '" + text.replace("'", "''") + "'\nexit %d\n" % exit_code, encoding="utf-8")


def ratio_extract_helper(tmp_path):
    source = RATIO_WORKSTREAM.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^function Test-RatioDispatchPermission \{.*?^\}", source)
    assert match
    harness = tmp_path / "ratio-helper.ps1"
    harness.write_text("param($Guard,$Kind)\n$ProductRatioGuard=$Guard\n$RepoRoot='x'\n" + match.group(0) + "\nexit (Test-RatioDispatchPermission -Kind $Kind)\n", encoding="utf-8")
    return harness


def ratio_run_helper(tmp_path, payload, kind="factory", exit_code=0, raw=None):
    helper = ratio_extract_helper(tmp_path)
    guard = tmp_path / "ratio-fake-guard.ps1"
    ratio_write_guard(guard, payload, exit_code=exit_code, raw=raw)
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(helper), "-Guard", str(guard), "-Kind", kind], text=True, capture_output=True)


def test_ratio_dispatch_read_failure_is_named_partial_red_and_never_uses_legacy(tmp_path):
    repo = ratio_source_init_repo(tmp_path / "repo")
    ratio_source_commit(repo, "docs", 2_000_000_000 - 2, {"docs/a": "a"})
    ratio_source_commit(repo, "fix product (#101)", 2_000_000_000 - 1, {"src/a": "a"})
    reservations = tmp_path / "reservations"
    reservations.mkdir()
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({"dispatchedUtc": "2033-05-18T03:33:19+00:00"}) + "\n", encoding="utf-8")
    command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(RATIO_GUARD), "-RepoRoot", str(repo), "-SourceRef", "master", "-AsOfEpoch", "2000000000", "-ReservationsPath", str(reservations), "-LegacyDispatchPath", str(legacy)]
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dispatchEvidenceSource"] == "unavailable"
    assert payload["dispatchCoverage"] == "PARTIAL"
    assert payload["dispatchEvidenceAvailable"] is False
    assert payload["dispatchesObserved"] == 0
    assert payload["recognizedProductPrIds"] == [101]
    assert payload["dispatchesPerLandedProductPr7dLowerBound"] is None
    assert payload["malformedDispatchRows"] == 0
    assert payload["verdict"] == "RED"
    assert "UNAVAILABLE_DISPATCH_EVIDENCE" in payload["reasons"]


@pytest.mark.parametrize("rate, expected", [(0.0, 3), (None, 0)])
def test_ratio_unavailable_evidence_requires_null_rate_for_product_admission(tmp_path, rate, expected):
    payload = ratio_guard_payload(dispatchEvidenceSource="unavailable",
        dispatchEvidenceAvailable=False, dispatchesObserved=0,
        dispatchesPerLandedProductPr7dLowerBound=rate,
        reasons=["UNAVAILABLE_DISPATCH_EVIDENCE", "RED_DISPATCH_COVERAGE_PARTIAL"])
    assert ratio_run_helper(tmp_path, payload, kind="product").returncode == expected


def test_ratio_empty_window_has_null_share_and_valid_red_is_typed_allowed(tmp_path):
    payload = ratio_guard_payload(commitPopulation=0, productCommitCount=0, productShare7d=None, recognizedProductPrCount=0, recognizedProductPrIds=[], hasProductLandings=False, dispatchesObserved=0, dispatchesPerLandedProductPr7dLowerBound=None, reasons=["RED_PRODUCT_SHARE", "RED_DISPATCH_COVERAGE_PARTIAL", "NO_PRODUCT_LANDINGS"])
    assert ratio_run_helper(tmp_path, payload, kind="product").returncode == 0
    assert ratio_run_helper(tmp_path, payload, kind="playback").returncode == 0
    assert ratio_run_helper(tmp_path, payload, kind="factory").returncode == 6


@pytest.mark.parametrize("mutation", [
    {"dispatchCoverage": "MAYBE"},
    {"verdict": "AMBER"},
    {"commitPopulation": "10"},
    {"productShare7d": float("nan")},
    {"productShare7d": 1.2},
    {"productCommitCount": 7},
    {"recognizedProductPrCount": 2},
    {"landingProvenanceComplete": False},
    {"dispatchEvidenceAvailable": False},
    {"unexpected": True},
    {"productShare7d": "0.6"},
    {"productShare7d": None, "productCommitCount": 0},
    {"productShareThreshold": "0.5"},
    {"dispatchRateThreshold": None},
    {"dispatchesPerLandedProductPr7dLowerBound": "5"},
    {"sourceSha": "not-a-sha"},
])
def test_ratio_unknown_types_ranges_and_count_contradictions_fail_closed(tmp_path, mutation):
    payload = ratio_guard_payload()
    payload.update(mutation)
    assert ratio_run_helper(tmp_path, payload, kind="product").returncode == 3


def test_ratio_contradictory_green_fails_closed_and_complete_green_passes(tmp_path):
    green = ratio_guard_payload(dispatchCoverage="COMPLETE", dispatchesObserved=4, dispatchesPerLandedProductPr7dLowerBound=4.0, verdict="GREEN", reasons=[])
    assert ratio_run_helper(tmp_path, green, kind="factory").returncode == 0
    partial = dict(green, dispatchCoverage="PARTIAL")
    assert ratio_run_helper(tmp_path, partial, kind="factory").returncode == 3
    missing_provenance = dict(green, landingProvenanceComplete=False)
    assert ratio_run_helper(tmp_path, missing_provenance, kind="factory").returncode == 3


def test_ratio_error_exit_three_blocks_every_kind(tmp_path):
    payload = ratio_guard_payload(sourceSha="", commitPopulation=0, productCommitCount=0, productShare7d=None, recognizedProductPrCount=0, recognizedProductPrIds=[], landingProvenanceComplete=False, hasProductLandings=False, dispatchEvidenceSource="unavailable", dispatchEvidenceAvailable=False, dispatchesObserved=0, dispatchesPerLandedProductPr7dLowerBound=None, verdict="ERROR", reasons=["ERROR_GIT_HISTORY"], errorCode="ERROR_GIT_HISTORY")
    for kind in ("factory", "product", "playback", ""):
        assert ratio_run_helper(tmp_path, payload, kind=kind, exit_code=3).returncode == 3


def ratio_full_dispatch_board(tmp_path, guard_payload, use_real_guard=False):
    dual, _ = editing_board(tmp_path, with_lane_shim=True)
    guard = tmp_path / "fake-product-ratio-guard.ps1"
    ratio_write_guard(guard, guard_payload)
    # The dispatcher resolves the guard beside itself; use a copied dispatcher plus fake guard,
    # while retaining fake lane/wrapper fixtures and a synthetic queue.
    tool_dir = tmp_path / "tools" / "coordination"
    tool_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RATIO_WORKSTREAM, tool_dir / "Invoke-Workstream.ps1")
    shutil.copy2(RATIO_GUARD if use_real_guard else guard, tool_dir / "Test-ProductRatioGuard.ps1")
    for dependency in ("landing-probe.ps1", "compose-lane-prompt-core.ps1", "repo-slug.ps1", "Retire-LaneWorktree.ps1", "lane-fsmonitor.ps1", "lane-dirty-save.ps1",
                       "Get-DoctrineBrief.ps1", "get_doctrine_brief.py"):
        shutil.copy2(RATIO_WORKSTREAM.parent / dependency, tool_dir / dependency)
    # get_doctrine_brief.py honours a fixture only inside the fixtures/ beside itself, so the
    # copied getter needs its own copy of the offline tree (the env is pointed at it below).
    shutil.copytree(RATIO_WORKSTREAM.parent / "fixtures" / "doctrine-offline",
                    tool_dir / "fixtures" / "doctrine-offline")
    (tool_dir / "Invoke-Lane.ps1").write_text("param($Lane,$PromptFile,$Card,$RunDir,$TimeoutSec)\nWrite-Output 'RATIO_FAKE_LANE'\nexit 0\n", encoding="ascii")
    (tool_dir / "Export-PrReviewEvidence.ps1").write_text("exit 0\n", encoding="ascii")
    return dual, tool_dir / "Invoke-Workstream.ps1"


@pytest.mark.parametrize("use_real_guard", [False, True])
def test_ratio_full_dispatcher_read_only_and_editing_apply_valid_red(tmp_path, use_real_guard):
    red = ratio_guard_payload()
    dual, dispatcher = ratio_full_dispatch_board(tmp_path, red, use_real_guard=use_real_guard)
    procedure = write_fields_card(dual / "prompts" / "v2", "RATIO-EDIT-PRODUCT")
    factory_procedure = write_fields_card(dual / "prompts" / "v2", "RATIO-EDIT-FACTORY")
    queue = tmp_path / "queue.json"
    items = [
        {"id": "RATIO-READ-PRODUCT", "state": "queued", "track": "product", "kind": "product", "owner": "sonnet", "priority": 1},
        {"id": "RATIO-READ-FACTORY", "state": "queued", "track": "factory", "kind": "factory", "owner": "sonnet", "priority": 1},
        {"id": "RATIO-EDIT-PRODUCT", "state": "queued", "track": "product", "kind": "product", "owner": "sonnet", "priority": 1, "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-RATIO-EDIT-PRODUCT.md", "procedureSha256": sha256_of(procedure)},
        {"id": "RATIO-EDIT-FACTORY", "state": "queued", "track": "factory", "kind": "factory", "owner": "sonnet", "priority": 1, "procedure": ".claude-state/coordination/dual-lane/prompts/v2/fields-RATIO-EDIT-FACTORY.md", "procedureSha256": sha256_of(factory_procedure)},
    ]
    queue.write_text(json.dumps({"schema": "test", "items": items}), encoding="utf-8")
    queue_before = queue.read_bytes()
    base = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(dispatcher), "-QueuePath", str(queue), "-NoLandingProbe", "-TimeoutSec", "1"]
    env = editing_dispatch_env(tmp_path)
    env["MLV_DOCTRINE_FIXTURE_ROOT"] = str(dispatcher.parent / "fixtures" / "doctrine-offline")
    read_product = subprocess.run(base + ["-CardId", "RATIO-READ-PRODUCT"], text=True, capture_output=True, env=env)
    read_factory = subprocess.run(base + ["-CardId", "RATIO-READ-FACTORY"], text=True, capture_output=True, env=env)
    try:
        edit_product = subprocess.run(base + ["-CardId", "RATIO-EDIT-PRODUCT", "-AllowEdits"], text=True, capture_output=True, env=env)
        edit_factory = subprocess.run(base + ["-CardId", "RATIO-EDIT-FACTORY", "-AllowEdits"], text=True, capture_output=True, env=env)
        assert read_product.returncode == 0 and "RATIO_FAKE_LANE" in read_product.stdout, read_product.stdout + read_product.stderr
        assert read_factory.returncode == 6 and "REFUSED ratio-guard-red kind=factory" in read_factory.stdout
        assert edit_product.returncode == 0 and "SHIM: lane=sonnet card=RATIO-EDIT-PRODUCT" in edit_product.stdout, edit_product.stdout + edit_product.stderr
        assert edit_factory.returncode == 6 and "REFUSED ratio-guard-red kind=factory" in edit_factory.stdout, edit_factory.stdout + edit_factory.stderr
        assert queue.read_bytes() == queue_before
        reserved = [json.loads(line) for line in (dual / "receipts" / "dispatch-reservations.jsonl").read_text().splitlines() if json.loads(line)["state"] == "reserved"]
        assert {row["card"] for row in reserved} == {"RATIO-READ-PRODUCT", "RATIO-EDIT-PRODUCT"}
        assert len(reserved) == 2
    finally:
        cleanup_lane_worktree(tmp_path, "RATIO-EDIT-PRODUCT")
        cleanup_lane_worktree(tmp_path, "RATIO-EDIT-FACTORY")


def test_ratio_actual_loop_foreach_body_skips_factory_exit_six_then_counts_product_zero(tmp_path):
    extractor = tmp_path / "extract-ratio-loop.ps1"
    extractor.write_text("param($Source)\n$tokens=$null;$errors=$null\n"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)\n"
        "if($errors.Count){throw 'loop parse failed'}\n"
        "$loops=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.ForEachStatementAst] -and $node.Variable.VariablePath.UserPath -eq 'track'},$true))\n"
        "if($loops.Count -ne 1){throw 'expected one track loop'}\n$loops[0].Extent.Text\n", encoding="utf-8")
    extracted = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(extractor), "-Source", str(RATIO_LOOP)], text=True, capture_output=True)
    assert extracted.returncode == 0, extracted.stderr
    body = extracted.stdout
    dispatcher = tmp_path / "ratio-fake-dispatcher.ps1"
    dispatcher.write_text("param($Track)\nif($Track -eq 'factory'){ Write-Output 'WORKSTREAM: REFUSED ratio-guard-red kind=factory'; exit 6 }; Write-Output ('WORKSTREAM: track=' + $Track + ' card=RATIO-NEXT'); exit 0\n", encoding="ascii")
    harness = tmp_path / "ratio-loop-body.ps1"
    harness.write_text("$Tracks=@('factory','product')\n$dispatched=@()\n$skipped=@()\n$MaxDispatchesPerCycle=1\n$DailyBudget=9\n$spentToday=0\n$Dispatcher='" + str(dispatcher).replace("'", "''") + "'\n$TimeoutSec=1\n$StaleHours=1\n$Lane=''\n$AllowEdits=$false\n$DryRun=$false\n" + body + "\n[ordered]@{dispatched=$dispatched;skipped=$skipped}|ConvertTo-Json -Depth 6 -Compress\n", encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert [row["track"] for row in payload["dispatched"]] == ["product"]
    assert payload["skipped"][0]["track"] == "factory"
    assert payload["skipped"][0]["reason"] == "exit-6"


# --- the refund rule: a dispatch that FAILED and spent NOTHING is refunded ------------------
# Measured 2026-09-05: two of twelve dispatches hit HTTP 429 "You've hit your session limit",
# and the loop then halted the rest of the UTC day at "daily budget exhausted (12/12)" having
# done ten units of work. The budget caps CONSUMPTION, not success -- so the test is
# costUsd == 0, NOT "it failed": CITE-TXN-1 failed after burning USD 2.24 and must still be
# charged, or the loop gets to spend the same money twice. And costReported must be TRUE,
# because an unreported cost read as free is the permissive branch of a fail-open.

LOOP_SCRIPT = ROOT / "tools" / "coordination" / "Invoke-WorkstreamLoop.ps1"


def test_the_dispatch_log_records_the_lane_spend_beside_its_exit_code():
    # Keep operator-visible spend in the legacy log. Budget authority is the
    # reservation ledger plus the bound receipt, not these copied log fields.
    body = WORKSTREAM.read_text(encoding="utf-8")
    assert "laneCostUsd" in body, "dispatch-log row does not carry the lane cost"
    assert "laneCostReported" in body, "dispatch-log row does not carry cost reportedness"


def test_an_unreadable_receipt_leaves_the_cost_UNREPORTED_not_zero():
    # Failing to read a cost must never look like a zero cost.
    body = WORKSTREAM.read_text(encoding="utf-8")
    assert "$laneCostReported = $false" in body, "cost reportedness does not default to false"


def test_the_refund_requires_all_three_conditions(tmp_path):
    rows = [_row('ZERO', 1, 0), _row('SUCCESS', 0, 0),
            _row('UNKNOWN', 1, 0, reported=False), _row('PAID', 1, 0.01)]
    assert _budget_of(tmp_path, rows) == 3


def test_the_refund_is_announced_rather_than_silent():
    # A budget that silently un-spends itself is indistinguishable from a miscount.
    assert "LOOP: refunded dispatch" in LOOP_SCRIPT.read_text(encoding="utf-8")


def _run_reservation_budget(tmp_path, rows):
    ledger = tmp_path / 'dispatch-reservations.jsonl'
    ledger.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
    harness = tmp_path / 'run-budget.ps1'
    harness.write_text('''param($Source,$Ledger,$Board)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'source parse failed'}
$functions=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-ReservationBudget'},$true))
if($functions.Count -ne 1){throw 'shipped budget function missing or ambiguous'}
$calls=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-ReservationBudget'},$true))
if($calls.Count -ne 1){throw 'loop must call the shipped reducer'}
Invoke-Expression $functions[0].Extent.Text
$spent=Get-ReservationBudget -LedgerPath $Ledger -TodayUtc ([datetime]'2026-09-05T12:00:00Z') -BoardRoot $Board
Write-Output ('SPENT=' + $spent)
''', encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(harness),
        '-Source', str(LOOP_SCRIPT), '-Ledger', str(ledger), '-Board', str(tmp_path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return int(result.stdout.split('SPENT=')[-1].strip())


def _reservation_fixture(tmp_path, index=0, exit_code=1, cost=0.0, reported=True):
    # Disposable actual-schema evidence. These are fixtures, never live lane receipts.
    run = tmp_path / '.claude-state' / 'fleet-runs' / f'fixture-{index}'
    run.mkdir(parents=True, exist_ok=True)
    receipt_path = run / 'sonnet-001.receipt.json'
    receipt = {'schema': 'mlv-app/fleet-lane-receipt/v1', 'lane': 'sonnet', 'card': f'CARD-{index}',
        'startedUtc': '2026-09-05T09:00:01Z', 'endedUtc': '2026-09-05T09:00:02Z',
        'exitCode': exit_code, 'spend': {'costReported': reported, 'costUsd': cost},
        'promptPath': str(run / 'sonnet-001.prompt.txt'), 'outputPath': str(run / 'sonnet-001.last.txt')}
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    reserved = {'reservationId': f'ID-{index}', 'state': 'reserved', 'lane': 'sonnet', 'card': f'CARD-{index}',
        'runDir': str(run), 'recordedUtc': '2026-09-05T09:00:00Z'}
    terminal = {**reserved, 'state': 'refunded', 'recordedUtc': '2026-09-05T09:00:03Z',
        'receiptPath': str(receipt_path), 'receiptSha256': sha256_of(receipt_path),
        'laneExitCode': exit_code, 'laneCostReported': reported, 'laneCostUsd': cost}
    return reserved, terminal, receipt, receipt_path


def _budget_of(tmp_path, rows):
    # Exercise the production reservation reducer using the historical cases'
    # exit/cost facts, translated into the new receipt-bound ledger contract.
    ledger = []
    for index, row in enumerate(rows):
        reserved, terminal, _, _ = _reservation_fixture(tmp_path, index, row['laneExitCode'],
            row['laneCostUsd'], row['laneCostReported'])
        ledger.extend([reserved, terminal])
    return _run_reservation_budget(tmp_path, ledger)



def _row(card, exit_code, cost, reported=True):
    return {
        "cardId": card, "dispatchedUtc": "2026-09-05T03:49:26.0000000Z",
        "laneExitCode": exit_code, "laneCostUsd": cost, "laneCostReported": reported,
    }


def test_the_three_real_receipts_from_20260905_score_correctly(tmp_path):
    rows = [
        _row("GATE-FAMILY-BOOKED", 1, 0.0),        # 429 in 2.8s, bought nothing -> REFUND
        _row("CITE-TXN-1", 1, 2.244599),           # failed, but USD 2.24 is gone -> CHARGE
        _row("CLEANUP-1", 0, 2.048145),            # succeeded                    -> CHARGE
    ]
    assert _budget_of(tmp_path, rows) == 2


def test_a_failed_but_EXPENSIVE_lane_is_still_charged(tmp_path):
    assert _budget_of(tmp_path, [_row("X", 1, 2.24)]) == 1


def test_an_unreported_cost_is_never_treated_as_free(tmp_path):
    assert _budget_of(tmp_path, [_row("X", 1, None, reported=False)]) == 1


def test_a_successful_free_lane_is_still_charged(tmp_path):
    # exit 0 means work happened, whatever it cost.
    assert _budget_of(tmp_path, [_row("X", 0, 0.0)]) == 1


@pytest.mark.parametrize('mutation', [
    'charged', 'duplicate_terminal', 'duplicate_reserved', 'cross_day_terminal',
    'missing_receipt', 'wrong_hash', 'changed_receipt', 'foreign_lane', 'foreign_card',
    'foreign_prompt', 'foreign_output', 'missing_run', 'foreign_terminal_run',
    'bool_exit', 'string_cost', 'bool_cost', 'null_cost', 'string_reported',
    'receipt_before_reservation', 'receipt_after_terminal', 'copied_cost_mismatch',
])
def test_reservation_refund_rejects_ambiguous_or_unbound_evidence(tmp_path, mutation):
    reserved, terminal, receipt, path = _reservation_fixture(tmp_path)
    rows = [reserved, terminal]
    if mutation == 'charged': terminal['state'] = 'charged'
    elif mutation == 'duplicate_terminal': rows.append(dict(terminal))
    elif mutation == 'duplicate_reserved': rows.append(dict(reserved))
    elif mutation == 'cross_day_terminal': rows.append({**terminal, 'recordedUtc': '2026-09-06T01:00:00Z'})
    elif mutation == 'missing_receipt': path.unlink()
    elif mutation == 'wrong_hash': terminal['receiptSha256'] = '0' * 64
    elif mutation == 'changed_receipt': path.write_text('{}')
    elif mutation == 'foreign_lane': receipt['lane'] = 'opus'
    elif mutation == 'foreign_card': receipt['card'] = 'FOREIGN'
    elif mutation == 'foreign_prompt': receipt['promptPath'] = str(tmp_path / 'foreign.prompt.txt')
    elif mutation == 'foreign_output': receipt['outputPath'] = str(tmp_path / 'foreign.last.txt')
    elif mutation == 'missing_run': reserved.pop('runDir')
    elif mutation == 'foreign_terminal_run': terminal['runDir'] = str(tmp_path / 'foreign')
    elif mutation == 'bool_exit': receipt['exitCode'] = True
    elif mutation == 'string_cost': receipt['spend']['costUsd'] = '0'
    elif mutation == 'bool_cost': receipt['spend']['costUsd'] = False
    elif mutation == 'null_cost': receipt['spend']['costUsd'] = None
    elif mutation == 'string_reported': receipt['spend']['costReported'] = 'true'
    elif mutation == 'receipt_before_reservation': receipt['startedUtc'] = '2026-09-05T08:00:00Z'
    elif mutation == 'receipt_after_terminal': receipt['endedUtc'] = '2026-09-05T10:00:00Z'
    elif mutation == 'copied_cost_mismatch': terminal['laneCostUsd'] = 1
    if mutation.startswith(('foreign_lane', 'foreign_card', 'foreign_prompt', 'foreign_output',
                            'bool_', 'string_', 'null_', 'receipt_')):
        path.write_text(json.dumps(receipt), encoding='utf-8')
        terminal['receiptSha256'] = sha256_of(path)
    assert _run_reservation_budget(tmp_path, rows) == 1


def test_malformed_reservation_ids_each_count_without_sentinel_collision(tmp_path):
    reserved, _, _, _ = _reservation_fixture(tmp_path)
    rows = [{**reserved, 'reservationId': value} for value in (None, '', 23, '__malformed__0')]
    assert _run_reservation_budget(tmp_path, rows) == 4


def test_reservation_budget_preserves_utc_boundary_and_counts_unresolved_once(tmp_path):
    reserved, _, _, _ = _reservation_fixture(tmp_path)
    rows = [reserved, dict(reserved),
        {**reserved, 'reservationId': 'PREVIOUS', 'recordedUtc': '2026-09-04T23:59:59Z'},
        {**reserved, 'reservationId': 'OFFSET', 'recordedUtc': '2026-09-04T20:00:00-05:00'}]
    assert _run_reservation_budget(tmp_path, rows) == 2


@pytest.mark.parametrize('bad_row', [None, [], {'state': 'mystery', 'recordedUtc': '2026-09-05T09:00:00Z'},
    {'state': 'reserved', 'recordedUtc': 'not-a-date'}])
def test_malformed_ledger_cannot_create_budget(tmp_path, bad_row):
    with pytest.raises(AssertionError):
        _run_reservation_budget(tmp_path, [bad_row])


@pytest.mark.parametrize('exit_code,cost,reported,expected', [
    (1, 0, True, 'refunded'), (0, 0, True, 'charged'), (1, 2.24, True, 'charged'),
    (1, None, False, 'charged'), (1, '0', True, 'charged'), (1, False, True, 'charged'),
    (1, 0, 'true', 'charged'),
])
def test_dispatch_terminal_uses_actual_typed_receipt_bytes(tmp_path, exit_code, cost, reported, expected):
    reserved, _, _, path = _reservation_fixture(tmp_path, exit_code=exit_code, cost=cost, reported=reported)
    harness = tmp_path / 'terminal-writer.ps1'
    harness.write_text('''param($Source,$Board,$Run,$ExitCode)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$RepoRoot=$Board
$ReservationsPath=Join-Path $Board 'writer-ledger.jsonl'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'source parse failed'}
foreach($name in @('Get-DispatchSpendEvidence','Write-DispatchReservation')) {
    $found=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    if($found.Count -ne 1){throw 'writer function missing or ambiguous'}
    Invoke-Expression $found[0].Extent.Text
}
$row=Write-DispatchReservation -ReservationId 'TEST-ID' -State charged -Card 'CARD-0' -Kind product -Lane sonnet -RunDir $Run -ObservedExit ([int]$ExitCode)
$row|ConvertTo-Json -Depth 6 -Compress
''', encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(harness),
        '-Source', str(WORKSTREAM), '-Board', str(tmp_path), '-Run', reserved['runDir'],
        '-ExitCode', str(exit_code)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    terminal = json.loads(result.stdout.strip().splitlines()[-1])
    assert terminal['state'] == expected
    assert terminal['runDir'] == reserved['runDir']
    assert json.loads((tmp_path / 'writer-ledger.jsonl').read_text(encoding='utf-8-sig')) == terminal
    if expected == 'refunded':
        assert terminal['receiptSha256'] == sha256_of(path)
        assert (tmp_path / terminal['receiptPath']).resolve() == path.resolve()
        assert terminal['laneCostReported'] is True and terminal['laneCostUsd'] == 0


# --- TOOL-GUARD-COVERAGE-ARM-UNSATISFIABLE-1: COMPLETE coverage through the REAL reader. Plan 0.6
# ratified that unfreezing needs seven days of version-enforced all-venue accounting; before this,
# Read-DispatchEvidence returned PARTIAL on every path, so every GREEN test injected COMPLETE.
# These cases drive the actual guard against a disposable repo, ledger and receipt tree.

COVERAGE_WINDOW_START = RATIO_SOURCE_AS_OF - 7 * RATIO_SOURCE_DAY
COVERAGE_LANDED = RATIO_SOURCE_AS_OF - 20 * RATIO_SOURCE_DAY


def coverage_stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def coverage_repo(tmp_path, landed=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, _ = fixture_template(("coverage_repo", landed), lambda r: _build_coverage_repo(r, landed))
    repo = tmp_path / "repo"
    shutil.copytree(root / "repo", repo)
    return repo


def _build_coverage_repo(tmp_path, landed=True):
    """Share 6/10 over the window, two recognized product PRs, so only coverage and rate decide."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = ratio_source_init_repo(tmp_path / "repo")
    # A root commit outside the window, so every in-window landing has a first parent.
    ratio_source_commit(repo, "base", RATIO_SOURCE_AS_OF - 30 * RATIO_SOURCE_DAY, {"docs/base.txt": "base"})
    # The ledger writer "lands" on the source ref before the window; rows older than this commit never start the clock.
    if landed:
        ratio_source_commit(repo, "land ledger writer", COVERAGE_LANDED, {"tools/coordination/Invoke-Lane.ps1": "function Add-DispatchLedgerRow([string]$Path, $Row) { } # MLV-DISPATCH-LEDGER-WRITER-V3-SERIALIZED\n"})
    for index in range(10):
        if index < 6:
            subject, path = f"product change {index} (#{101 + index % 2})", f"src/p{index}.txt"
        else:
            subject, path = f"docs {index}", f"docs/d{index}.txt"
        ratio_source_commit(repo, subject, RATIO_SOURCE_AS_OF - RATIO_SOURCE_DAY + index, {path: str(index)})
    return repo


def coverage_receipt(fleet, name, epoch, *, text=None, schema="mlv-app/fleet-lane-receipt/v1"):
    path = fleet / "run" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        text = json.dumps({"schema": schema, "state": "complete", "lane": "sol", "startedUtc": coverage_stamp(epoch)})
    path.write_text(text, encoding="utf-8")
    os.utime(path, (epoch, epoch))
    return path


def coverage_row(epoch, state="reserved", receipt=None, version=2, **extra):
    row = {"state": state, "recordedUtc": coverage_stamp(epoch), "lane": "sol", "card": "C"}
    if version is not None:
        row["schemaVersion"] = version
    if receipt is not None:
        row["receiptPath"] = str(receipt)
    row.update(extra)
    return json.dumps(row)


def coverage_guard(repo, ledger, fleet):
    command = [
        "pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(RATIO_SOURCE_GUARD), "-RepoRoot", str(repo), "-SourceRef", "master",
        "-AsOfEpoch", str(RATIO_SOURCE_AS_OF), "-ReservationsPath", str(ledger),
        "-LegacyDispatchPath", str(ledger.parent / "absent-legacy.jsonl"), "-FleetRunsPath", str(fleet),
    ]
    result = ratio_source_run(*command)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def coverage_enforced_fixture(tmp_path, landed=True):
    repo = coverage_repo(tmp_path, landed=landed)
    fleet = tmp_path / "fleet-runs"
    fleet.mkdir()
    first = coverage_receipt(fleet, "sol-001.receipt.json", RATIO_SOURCE_AS_OF - 3600)
    second = coverage_receipt(fleet, "sol-002.receipt.json", RATIO_SOURCE_AS_OF - 1800)
    rows = [
        # Enforcement began before the window: the first versioned row predates windowStart.
        coverage_row(COVERAGE_WINDOW_START - 60, state="charged"),
        coverage_row(RATIO_SOURCE_AS_OF - 3600, receipt=first),
        coverage_row(RATIO_SOURCE_AS_OF - 1800, state="linked", receipt=second, reservationId="R-1", venue="invoke-lane"),
        # The dispatcher reservation the linked row above names.
        coverage_row(RATIO_SOURCE_AS_OF - 1801, reservationId="R-1", venue="invoke-workstream"),
    ]
    return repo, fleet, rows, first, second


def test_ratio_coverage_complete_goes_green_through_the_real_reader(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["dispatchCoverage"] == "COMPLETE", payload["reasons"]
    # The linked row names a receipt but is not a second launch.
    assert payload["dispatchesObserved"] == 2
    assert payload["recognizedProductPrIds"] == [101, 102]
    assert payload["verdict"] == "GREEN", (payload["reasons"], payload["productShare7d"], payload["dispatchesPerLandedProductPr7dLowerBound"])
    assert payload["reasons"] == []


def test_ratio_coverage_unreserved_invoke_lane_receipt_stays_red(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    coverage_receipt(fleet, "opus-001.receipt.json", RATIO_SOURCE_AS_OF - 900)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["dispatchCoverage"] == "PARTIAL"
    assert payload["verdict"] == "RED"
    assert "COVERAGE_RECEIPT_UNRESERVED" in payload["reasons"]


def test_ratio_coverage_window_that_starts_before_enforcement_stays_red(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows[1:]) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["verdict"] == "RED"
    assert "COVERAGE_WINDOW_PREDATES_ENFORCEMENT" in payload["reasons"]


def test_ratio_coverage_unversioned_row_after_enforcement_stays_red(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows + [coverage_row(RATIO_SOURCE_AS_OF - 600, version=None)]) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["verdict"] == "RED"
    assert "COVERAGE_UNVERSIONED_ROW_AFTER_ENFORCEMENT" in payload["reasons"]


def test_ratio_coverage_legacy_only_ledger_is_not_enforced_and_not_malformed(tmp_path):
    repo, fleet, _, _, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    # Unversioned history, including an out-of-window row with no state, is legacy - never malformed.
    ledger.write_text("\n".join([
        coverage_row(COVERAGE_WINDOW_START - 999, version=None),
        json.dumps({"recordedUtc": coverage_stamp(COVERAGE_WINDOW_START - 998)}),
        coverage_row(RATIO_SOURCE_AS_OF - 10, version=None),
    ]) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["malformedDispatchRows"] == 0
    assert payload["dispatchesObserved"] == 1
    assert "COVERAGE_NOT_ENFORCED" in payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_missing_receipt_unreadable_and_in_flight_are_never_complete(tmp_path):
    repo, fleet, rows, first, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    first.unlink()
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_RESERVATION_RECEIPT_MISSING" in payload["reasons"]
    assert payload["verdict"] == "RED"

    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path / "b")
    coverage_receipt(fleet, "luna-001.receipt.json", RATIO_SOURCE_AS_OF - 100, text="")
    coverage_receipt(fleet, "luna-002.receipt.json", RATIO_SOURCE_AS_OF - 100, text="{not json")
    coverage_receipt(fleet, "luna-003.receipt.json", RATIO_SOURCE_AS_OF - 100, text=json.dumps({
        "schema": "mlv-app/fleet-lane-receipt/v1", "state": "reserved", "reservedUtc": coverage_stamp(RATIO_SOURCE_AS_OF - 100)}))
    ledger = tmp_path / "b" / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_RECEIPT_IN_FLIGHT" in payload["reasons"]
    assert "COVERAGE_RECEIPT_UNREADABLE" in payload["reasons"]
    assert "COVERAGE_RECEIPT_UNRESERVED" not in payload["reasons"], "a bare slot marker is in flight, not unreserved"
    assert payload["dispatchCoverage"] == "PARTIAL"

    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path / "c")
    ledger = tmp_path / "c" / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, tmp_path / "c" / "no-such-fleet-runs")
    assert "COVERAGE_RECEIPTS_UNAVAILABLE" in payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_ignores_other_schemas_pre_window_receipts_and_refused_launches(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    coverage_receipt(fleet, "other.receipt.json", RATIO_SOURCE_AS_OF - 50, schema="mlv-app/something-else/v1")
    coverage_receipt(fleet, "old.receipt.json", COVERAGE_WINDOW_START - 50)
    coverage_receipt(fleet, "refused.receipt.json", RATIO_SOURCE_AS_OF - 40, text=json.dumps({
        "schema": "mlv-app/fleet-lane-receipt/v1", "state": "failed", "startedUtc": coverage_stamp(RATIO_SOURCE_AS_OF - 40),
        "failure": "dispatch-ledger-write-failed: disk full"}))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["dispatchCoverage"] == "COMPLETE", payload["reasons"]
    assert payload["verdict"] == "GREEN"


def test_ratio_coverage_every_launch_venue_writes_a_versioned_ledger_row():
    lane = LANE_RUNNER.read_text(encoding="utf-8")
    ws = RATIO_WORKSTREAM.read_text(encoding="utf-8")
    # Invoke-Lane: the row names the receipt slot, is written before any provider starts, and a
    # failed write refuses the launch.
    slot = lane.index("[System.IO.FileMode]::CreateNew")
    row = lane.index("Add-DispatchLedgerRow -Path $DispatchLedgerPath")
    start = lane.index("[Diagnostics.Process]::Start($psi)")
    assert slot < row < start
    assert "throw \"dispatch-ledger-write-failed:" in lane
    assert "schemaVersion = 2" in lane and "receiptPath   = $rcptPath" in lane
    assert "[void]$psi.Environment.Remove('MLV_DISPATCH_RESERVATION_ID')" in lane
    # Invoke-Workstream: versioned rows, and the reservation id is handed to BOTH launch sites
    # (Invoke-Lane directly and Start-EditingLane) before launch and cleared after.
    assert "schemaVersion = 2" in ws and "venue         = 'invoke-workstream'" in ws
    assert ws.count("$env:MLV_DISPATCH_RESERVATION_ID = $reservationId") == 2
    assert ws.count("Remove-Item Env:\\MLV_DISPATCH_RESERVATION_ID") == 2
    for launch in ("-File $LaneRunner", "-File $StartEditingLane"):
        at = ws.index(launch)
        assert ws.rindex("$env:MLV_DISPATCH_RESERVATION_ID = $reservationId", 0, at) > ws.rindex("-State 'reserved'", 0, at)


# --- PR #127 round 2 (sol CHANGES_REQUESTED at 7e41d5b8): four ways COMPLETE could be reached without
# seven days of all-venue accounting. Each case must stay RED.

def test_ratio_coverage_rows_from_an_unlanded_writer_never_start_the_clock(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path, landed=False)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["verdict"] == "RED"
    assert "COVERAGE_NOT_ENFORCED" in payload["reasons"]


def test_ratio_coverage_rows_older_than_the_landing_commit_never_start_the_clock(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    # The only row that predates the window was written before the writer landed.
    rows[0] = coverage_row(COVERAGE_LANDED - 60, state="charged")
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_WINDOW_PREDATES_ENFORCEMENT" in payload["reasons"], payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_unmatched_or_duplicate_links_count_as_launches(tmp_path):
    repo, fleet, rows, _, second = coverage_enforced_fixture(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    extra = [
        coverage_row(RATIO_SOURCE_AS_OF - 1700, state="linked", receipt=second, reservationId="R-1", venue="invoke-lane"),
        coverage_row(RATIO_SOURCE_AS_OF - 1600, state="linked", reservationId="NO-SUCH-RESERVATION", venue="invoke-lane"),
    ]
    ledger.write_text("\n".join(rows + extra) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert payload["dispatchesObserved"] == 4, "a duplicate and an unmatched link are each a launch"
    assert "COVERAGE_LINK_UNMATCHED" in payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_a_receipt_with_an_old_file_time_is_still_judged_by_startedUtc(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    stale = coverage_receipt(fleet, "opus-009.receipt.json", RATIO_SOURCE_AS_OF - 500)
    os.utime(stale, (COVERAGE_WINDOW_START - 9999, COVERAGE_WINDOW_START - 9999))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_RECEIPT_UNRESERVED" in payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_a_stale_worktree_runner_keeping_receipts_in_its_worktree_is_seen(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    worktree = tmp_path / "wt"
    ratio_source_git(repo, "worktree", "add", "-q", str(worktree))
    coverage_receipt(worktree / ".claude-state" / "fleet-runs", "sonnet-001.receipt.json", RATIO_SOURCE_AS_OF - 300)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_RECEIPT_UNRESERVED" in payload["reasons"], payload["reasons"]
    assert payload["verdict"] == "RED"


# --- PR #127 round 3 (sol CHANGES_REQUESTED at 84ad327b).

def test_ratio_coverage_a_registered_checkout_with_a_stale_runner_is_never_complete(tmp_path):
    # -RunDir can point anywhere, so a stale runner's receipt may never be scanned; the runner itself is.
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    worktree = tmp_path / "old"
    ratio_source_git(repo, "worktree", "add", "-q", str(worktree))
    (worktree / "tools" / "coordination" / "Invoke-Lane.ps1").write_text("# a runner from before the ledger writer\n", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_STALE_RUNNER_PRESENT" in payload["reasons"], payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_a_runner_replaced_inside_the_window_is_never_complete(tmp_path):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    runner = repo / "tools" / "coordination" / "Invoke-Lane.ps1"
    os.utime(runner, (RATIO_SOURCE_AS_OF - 3600, RATIO_SOURCE_AS_OF - 3600))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_RUNNER_UPDATED_IN_WINDOW" in payload["reasons"], payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_ledger_writers_in_both_launchers_never_lose_a_row_under_contention(tmp_path):
    # FileMode.Append snapshots EOF per stream, so unserialized concurrent appends can overwrite rows.
    # Both writers must take the same machine-wide mutex: 4 processes x 40 rows, mixed writers.
    ledger = tmp_path / "ledger.jsonl"
    harness = tmp_path / "writer.ps1"
    harness.write_text('''param($Lane,$Workstream,$Ledger,$Which,$Count)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$ReservationsPath=$Ledger
$tokens=$null;$errors=$null
foreach($pair in @(@($Lane,'Add-DispatchLedgerRow'),@($Workstream,'Write-DispatchReservation'))) {
    $ast=[Management.Automation.Language.Parser]::ParseFile($pair[0],[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'source parse failed'}
    $found=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $pair[1]},$true))
    if($found.Count -ne 1){throw "missing $($pair[1])"}
    Invoke-Expression $found[0].Extent.Text
}
for($i=0;$i -lt [int]$Count;$i++){
    if($Which -eq 'lane'){ Add-DispatchLedgerRow -Path $Ledger -Row ([ordered]@{schemaVersion=2;state='reserved';n=$i;pad=('x'*512);recordedUtc=(Get-Date).ToUniversalTime().ToString('o')}) }
    else { $null = Write-DispatchReservation -ReservationId "W-$PID-$i" -State reserved -Card C -Kind product -Lane sonnet -RunDir R }
}
''', encoding="utf-8")
    procs = [
        subprocess.Popen(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness), "-Lane", str(LANE_RUNNER),
                          "-Workstream", str(RATIO_WORKSTREAM), "-Ledger", str(ledger), "-Which", which, "-Count", "40"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for which in ("lane", "workstream", "lane", "workstream")
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, out + err
    lines = [line for line in ledger.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    assert len(lines) == 160
    for line in lines:
        json.loads(line)


def test_loop_budget_ignores_invoke_lane_ledger_rows_and_never_halts_on_linked(tmp_path):
    # PR #127: Invoke-Lane now writes 'reserved' (direct) and 'linked' rows for the ratio guard's
    # coverage. Before the fix, 'linked' threw 'unknown reservation state' in the loop's reducer,
    # halting every cycle, and direct hub/review launches would have spent the unattended budget.
    rows = [
        {'reservationId': 'W-1', 'state': 'reserved', 'schemaVersion': 2, 'venue': 'invoke-workstream',
         'lane': 'sonnet', 'card': 'C', 'runDir': 'R', 'recordedUtc': '2026-09-05T09:00:00Z'},
        {'reservationId': 'W-1', 'state': 'linked', 'schemaVersion': 2, 'venue': 'invoke-lane',
         'lane': 'sonnet', 'card': 'C', 'runDir': 'R', 'receiptPath': 'R/sonnet-001.receipt.json', 'recordedUtc': '2026-09-05T09:00:01Z'},
        {'reservationId': 'L-1', 'state': 'reserved', 'schemaVersion': 2, 'venue': 'invoke-lane',
         'lane': 'sol', 'card': 'REVIEW', 'runDir': 'R2', 'receiptPath': 'R2/sol-001.receipt.json', 'recordedUtc': '2026-09-05T10:00:00Z'},
    ]
    assert _run_reservation_budget(tmp_path, rows) == 1


@pytest.mark.parametrize("writer,text", [
    # The unserialized round-2 runner already had the writer function but no mutex (sol, PR #127 r3).
    ("Invoke-Lane.ps1", "function Add-DispatchLedgerRow([string]$Path, $Row) { [IO.File]::Open($Path, 'Append') }\n"),
    ("Invoke-Workstream.ps1", "function Write-DispatchReservation { Add-Content -LiteralPath $ReservationsPath -Value $row }\n"),
])
def test_ratio_coverage_a_writer_without_the_serialized_marker_is_stale_even_with_an_old_file_time(tmp_path, writer, text):
    repo, fleet, rows, _, _ = coverage_enforced_fixture(tmp_path)
    worktree = tmp_path / "unsafe"
    ratio_source_git(repo, "worktree", "add", "-q", str(worktree))
    target = worktree / "tools" / "coordination" / writer
    target.write_text(text, encoding="utf-8")
    os.utime(target, (COVERAGE_WINDOW_START - 9999, COVERAGE_WINDOW_START - 9999))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = coverage_guard(repo, ledger, fleet)
    assert "COVERAGE_STALE_RUNNER_PRESENT" in payload["reasons"], payload["reasons"]
    assert payload["verdict"] == "RED"


def test_ratio_coverage_marker_is_carried_by_both_shipped_writers_and_the_guard():
    marker = "MLV-DISPATCH-LEDGER-WRITER-V3-SERIALIZED"
    for path in (LANE_RUNNER, RATIO_WORKSTREAM, RATIO_SOURCE_GUARD):
        assert marker in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- HOST-FSMONITOR-DAEMON-LEAK-1
# MEASURED 2026-10-17 on Virtual-Ten: 65 `git fsmonitor--daemon run --detach` processes for 64 worktrees, the oldest 17 h old,
# ~51 MiB private each, host commit 61 GiB against a 60 GiB gate. The board's .git/config carries core.fsmonitor=true, every
# linked worktree inherits it, the first `git status` in a worktree starts a daemon, and nothing stopped it when the lane ended.
# (a) lane worktrees get a worktree-scoped core.fsmonitor=false at creation / launch; (b) retirement stops the daemon first.
FSMON_LIB = ROOT / "tools" / "coordination" / "lane-fsmonitor.ps1"
FSMON_PWSH = shutil.which("pwsh")
fsmon_only = pytest.mark.skipif(
    sys.platform != "win32" or FSMON_PWSH is None,
    reason="the builtin fsmonitor daemon and the PowerShell helpers are exercised on the Windows venue only",
)


def fsmon_pwsh(body):
    return subprocess.run(
        [FSMON_PWSH, "-NoProfile", "-NonInteractive", "-Command", f". '{FSMON_LIB}'; {body}"],
        text=True, capture_output=True,
    )


def fsmon_watching(path):
    done = subprocess.run(["git", "-C", str(path), "fsmonitor--daemon", "status"], text=True, capture_output=True)
    return "is watching" in (done.stdout + done.stderr)


@pytest.fixture
def fsmon_board(tmp_path):
    """A board with core.fsmonitor=true at repo scope (the live shape) and one linked lane worktree. Stops any daemon on exit."""
    main = tmp_path / "board"
    main.mkdir()
    make_repo(main)
    git(main, "config", "core.fsmonitor", "true")
    wt = tmp_path / "lane-wt"
    # -c core.fsmonitor=false: the fixture itself must not start the daemon it is about to measure.
    git(main, "-c", "core.fsmonitor=false", "worktree", "add", "-q", "-b", "lane", str(wt))
    try:
        yield main, wt
    finally:
        for p in (wt, main):
            subprocess.run(["git", "-C", str(p), "fsmonitor--daemon", "stop"], capture_output=True)


@fsmon_only
def test_fsmonitor_disable_is_worktree_scoped_and_a_status_then_starts_no_daemon(fsmon_board):
    main, wt = fsmon_board
    git(main, "config", "extensions.worktreeConfig", "true")
    done = fsmon_pwsh(f"(Disable-LaneWorktreeFsmonitor -WorkDir '{wt}').action")
    assert done.returncode == 0 and done.stdout.strip() == "disabled", (done.stdout, done.stderr)
    assert git(wt, "config", "--get", "core.fsmonitor") == "false"
    assert git(main, "config", "--local", "--get", "core.fsmonitor") == "true", "the board's own setting must be untouched"
    git(wt, "status", "--porcelain")
    assert not fsmon_watching(wt), "a status in the disabled worktree started a daemon"


@fsmon_only
def test_fsmonitor_control_a_status_in_an_untouched_worktree_does_start_a_daemon(fsmon_board):
    """The control: same fixture, no helper call. Proves the daemon the fix prevents is real, not an artifact."""
    main, wt = fsmon_board
    git(main, "config", "extensions.worktreeConfig", "true")
    git(wt, "status", "--porcelain")
    assert fsmon_watching(wt)


@fsmon_only
def test_fsmonitor_disable_refuses_to_write_the_shared_config_without_the_worktree_extension(fsmon_board):
    main, wt = fsmon_board
    done = fsmon_pwsh(f"(Disable-LaneWorktreeFsmonitor -WorkDir '{wt}') | ConvertTo-Json -Compress")
    assert done.returncode == 0, done.stderr
    assert '"action":"skipped"' in done.stdout and "extensions.worktreeConfig" in done.stdout, done.stdout
    assert git(main, "config", "--local", "--get", "core.fsmonitor") == "true"
    assert git(wt, "config", "--get", "core.fsmonitor") == "true", "without the extension nothing may be written"


@fsmon_only
def test_fsmonitor_helpers_never_touch_the_main_checkout(fsmon_board):
    main, wt = fsmon_board
    git(main, "config", "extensions.worktreeConfig", "true")
    git(main, "status", "--porcelain")  # starts the board's own daemon
    assert fsmon_watching(main)
    done = fsmon_pwsh(f"(Disable-LaneWorktreeFsmonitor -WorkDir '{main}').action; (Stop-LaneWorktreeFsmonitor -WorkDir '{main}').action")
    assert done.returncode == 0 and done.stdout.split() == ["skipped", "skipped"], (done.stdout, done.stderr)
    assert fsmon_watching(main), "the canonical board checkout's daemon was stopped"
    assert git(main, "config", "--local", "--get", "core.fsmonitor") == "true"


@fsmon_only
def test_fsmonitor_stop_ends_only_the_named_worktrees_daemon(fsmon_board):
    main, wt = fsmon_board
    git(main, "config", "extensions.worktreeConfig", "true")
    git(wt, "status", "--porcelain")
    git(main, "status", "--porcelain")
    assert fsmon_watching(wt) and fsmon_watching(main)
    done = fsmon_pwsh(f"(Stop-LaneWorktreeFsmonitor -WorkDir '{wt}').action")
    assert done.returncode == 0 and done.stdout.strip() == "stopped", (done.stdout, done.stderr)
    assert not fsmon_watching(wt)
    assert fsmon_watching(main)
    again = fsmon_pwsh(f"(Stop-LaneWorktreeFsmonitor -WorkDir '{wt}').action")
    assert again.stdout.strip() == "none", "a worktree with no daemon is a quiet no-op"


def test_fsmonitor_fix_is_wired_into_every_lane_worktree_path():
    coord = ROOT / "tools" / "coordination"
    retire = (coord / "Retire-LaneWorktree.ps1").read_text(encoding="utf-8")
    assert "lane-fsmonitor.ps1" in retire, "Retire-LaneWorktree.ps1 must load the helpers it calls"
    body = retire[retire.index("function Invoke-RetireLaneWorktree"):]
    # (b) the daemon is stopped before the worktree is removed, and (a) the worktree is switched off before the gate's first status.
    assert body.index("Disable-LaneWorktreeFsmonitor") < body.index("'status', '--porcelain', '-uall'")
    assert body.index("Stop-LaneWorktreeFsmonitor") < body.index("'worktree', 'remove'")
    for name in ("Invoke-Lane.ps1", "Invoke-Workstream.ps1", "Invoke-WorkstreamLoop.ps1"):
        assert "Disable-LaneWorktreeFsmonitor" in (coord / name).read_text(encoding="utf-8"), name
    # Every tracked `worktree add` also passes -c core.fsmonitor=false: the checkout inside `add` is itself a daemon starter.
    for name in ("Invoke-Workstream.ps1", "Invoke-WorkstreamLoop.ps1"):
        adds = [ln for ln in (coord / name).read_text(encoding="utf-8").splitlines() if "worktree add" in ln and ln.lstrip().startswith("&")]
        assert adds and all("core.fsmonitor=false" in ln for ln in adds), (name, adds)


# ---- r2 (sol r1 BLOCKER on d754de31): ORDER. The disable has to precede the first git command that reads the worktree's index; a disable that
# runs after the first `git status` leaves the daemon that status started. r1 pinned only that the call APPEARS in each script.
def test_fsmonitor_disable_precedes_the_first_git_command_in_invoke_lane():
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    disable = lane.index("$null = Disable-LaneWorktreeFsmonitor -WorkDir $WorkDir")
    first_git = re.search(r"^\$\w+\s*=\s*Invoke-GitCaptureUtf8\b", lane, re.M)
    assert first_git, "Invoke-Lane.ps1 no longer calls git at top level; re-pin this test on its first git command"
    assert disable < first_git.start(), "Invoke-Lane.ps1 runs git in the lane worktree before disabling its fsmonitor"
    assert "Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir" in lane[disable:first_git.start()], "a relaunched worktree's running daemon must be stopped too"


def test_fsmonitor_protect_precedes_every_git_command_on_the_reused_continuation_worktree():
    ws = (ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1").read_text(encoding="utf-8")
    protect = ws.index("$null = Protect-LaneWorktreeFsmonitor -WorkDir $existingWt")
    reuse_git = [m.start() for m in re.finditer(r"git -C \$existingWt\b", ws)]
    assert reuse_git, "the reuse path no longer runs git in $existingWt; re-pin this test"
    assert protect < min(reuse_git), "Invoke-Workstream.ps1 runs git in a reused worktree before protecting it"
    assert protect < ws.index("continuation-worktree-dirty"), "the dirty-worktree refusal (exit 6) must not be reachable before the protect"


def test_fsmonitor_disable_and_stop_precede_the_driver_worktree_reset():
    loop = (ROOT / "tools" / "coordination" / "Invoke-WorkstreamLoop.ps1").read_text(encoding="utf-8")
    sync = loop[loop.index("function Sync-DriverWorktree"):]
    created = sync.index("            return $null\n        }\n")  # end of the create-a-new-driver branch; what follows is the reuse path
    assert "Disable-DriverFsmonitor" in sync[created:sync.index("git -C $DriverRoot reset")], "the reused driver worktree is reset before its fsmonitor is disabled"
    helper = loop[loop.index("function Disable-DriverFsmonitor"):loop.index("function Sync-DriverWorktree")]
    assert "Disable-LaneWorktreeFsmonitor" in helper and "Stop-LaneWorktreeFsmonitor" in helper


@fsmon_only
def test_fsmonitor_protect_disables_and_stops_a_daemon_already_running(fsmon_board):
    main, wt = fsmon_board
    git(main, "config", "extensions.worktreeConfig", "true")
    git(wt, "status", "--porcelain")
    assert fsmon_watching(wt)
    done = fsmon_pwsh(f"$p = Protect-LaneWorktreeFsmonitor -WorkDir '{wt}'; $p.disable; $p.stop")
    assert done.returncode == 0 and done.stdout.split() == ["disabled", "stopped"], (done.stdout, done.stderr)
    assert not fsmon_watching(wt)
    git(wt, "status", "--porcelain")
    assert not fsmon_watching(wt), "a status after the protect started a daemon again"


def fsmon_continuation_board(tmp_path, monkeypatch, card_id, dirty, prestart):
    """A board with core.fsmonitor=true at repo scope and extensions.worktreeConfig on (the live shape), and a continuation branch checked out
    in an EXISTING worktree that predates the fix (no worktree-scoped override). prestart=True also leaves a daemon already running there."""
    dual, head = editing_board(tmp_path)
    git(tmp_path, "config", "extensions.worktreeConfig", "true")
    branch = "product/" + card_id
    git(tmp_path, "checkout", "-q", "-b", branch)
    (tmp_path / "stage1.txt").write_text("stage one\n")
    git(tmp_path, "add", "stage1.txt")
    git(tmp_path, "commit", "-qm", "stage one")
    tip = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "--detach", head)
    wt = tmp_path / ("wt-" + card_id.lower())
    git(tmp_path, "-c", "core.fsmonitor=false", "worktree", "add", "-q", str(wt), branch)
    git(tmp_path, "config", "core.fsmonitor", "true")
    if dirty:
        (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    if prestart:
        git(wt, "status", "--porcelain")
        assert fsmon_watching(wt), "the fixture failed to start the daemon it is about to measure"
    # The lane shim records the daemon state at the moment the lane would start, before the dispatcher's own cleanup can retire the worktree.
    probe = tmp_path / "fsmon-at-lane-start.txt"
    monkeypatch.setenv("MLV_FSMON_PROBE_OUT", str(probe))
    (dual / "Start-EditingLane.ps1").write_text(
        "param([string]$Lane,[string]$PromptFile,[string]$WorkDir,[string]$Card,[string]$RunDir,[string]$ExtraReadDir,[int]$TimeoutSec)\n"
        "$o = (& git -C $WorkDir fsmonitor--daemon status 2>&1 | Out-String)\n"
        "Set-Content -LiteralPath $env:MLV_FSMON_PROBE_OUT -Value ('PROBE ' + $o)\n"
        "exit 0\n", encoding="ascii")
    return dual, wt, tip, probe


def fsmon_continuation_cleanup(tmp_path, wt, card_id):
    for p in (wt, tmp_path):
        subprocess.run(["git", "-C", str(p), "fsmonitor--daemon", "stop"], capture_output=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path, check=False, capture_output=True)
    cleanup_lane_worktree(tmp_path, card_id)


@fsmon_only
@pytest.mark.parametrize("prestart", [False, True], ids=["no-daemon-yet", "daemon-already-running"])
@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty-exit6"])
def test_continuation_reuse_starts_and_leaves_no_fsmonitor_daemon(tmp_path, monkeypatch, dirty, prestart):
    """sol r1 BLOCKER: the reused-continuation path ran `git status` before disabling fsmonitor, so a worktree that predates the fix got a daemon
    on the dispatcher's own status; the dirty branch exits 6 before the later disable and never reached it at all."""
    card_id = "TEST-FSMON-CONT-%s-%s" % ("D" if dirty else "C", "P" if prestart else "N")
    dual, wt, tip, probe = fsmon_continuation_board(tmp_path, monkeypatch, card_id, dirty, prestart)
    try:
        result = run_editing_dispatch(tmp_path, [_reuse_card(dual, card_id)], card_id, extra=("-ContinueFromSha", tip), dry_run=False)
        if dirty:
            assert result.returncode == 6 and "REFUSED continuation-worktree-dirty" in result.stdout, result.stdout + result.stderr
            assert not fsmon_watching(wt), "the dirty-refusal path left a fsmonitor daemon running in the reused worktree"
            assert git(wt, "config", "--get", "core.fsmonitor") == "false"
        else:
            assert result.returncode == 0 and "reusing-worktree=" in result.stdout, result.stdout + result.stderr
            assert probe.exists(), "the lane shim never ran: " + result.stdout + result.stderr
            seen = probe.read_text(encoding="utf-8")
            assert "is watching" not in seen, "a fsmonitor daemon was running in the reused worktree when the lane started: " + seen
    finally:
        fsmon_continuation_cleanup(tmp_path, wt, card_id)


# ---------------------------------------------------------------- LANE-FSMONITOR-PIN-AND-RETIRE-HARDENING-1
# Two hardening findings left open by the #485 review.
# RETIRE-KEPT-WORKTREE-DAEMON-1: a worktree the retirement gate KEEPS (dirty, unpushed, held by a live process, ...) can hold a daemon that was
# already running before the fix landed; the daemon was stopped only on the remove path. It has to be stopped on every path that keeps it.
FSMON_RETIRE_LIB = ROOT / "tools" / "coordination" / "Retire-LaneWorktree.ps1"
FSMON_NO_PROCS = "[pscustomobject]@{ Procs = @(); SelfPids = @($PID) }"


def fsmon_retire(wt, snapshot=FSMON_NO_PROCS):
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{FSMON_RETIRE_LIB}'; $snap = {snapshot}; "
        f"Invoke-RetireLaneWorktree -WorkDir '{wt}' -MergeTarget 'master' -ProcessSnapshot $snap | ConvertTo-Json -Depth 4"
    )
    done = subprocess.run([FSMON_PWSH, "-NoProfile", "-NonInteractive", "-Command", script], text=True, capture_output=True)
    assert done.returncode == 0, (done.stdout, done.stderr)
    return json.loads(done.stdout)


@fsmon_only
@pytest.mark.parametrize(
    "extension,kept_reason,snapshot_for",
    [
        (True, "dirty", None),
        (True, "unpushed", None),
        (True, "live-process", lambda wt: "[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999999; Name = 'fake-editor.exe'; "
                                          f"CommandLine = 'fake-editor.exe --open {wt}' }}); SelfPids = @($PID) }}"),
        (False, "unpushed", None),
    ],
    ids=["dirty", "unpushed", "live-process", "unpushed-without-worktree-extension"],
)
def test_retire_stops_a_daemon_already_running_in_a_worktree_it_keeps(fsmon_board, extension, kept_reason, snapshot_for):
    """The fixture leaves a daemon running (a worktree that predates the fix). The fixture board has no remote, so any HEAD is `unpushed`. Without
    extensions.worktreeConfig the disable is skipped, so the gate's own status starts the daemon again: that case needs a stop at the end."""
    main, wt = fsmon_board
    if extension:
        git(main, "config", "extensions.worktreeConfig", "true")
    if kept_reason == "dirty":
        (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    git(wt, "status", "--porcelain")
    assert fsmon_watching(wt), "the fixture failed to start the daemon it is about to measure"
    d = fsmon_retire(wt, snapshot_for(wt) if snapshot_for else FSMON_NO_PROCS)
    assert d["action"] == "kept" and d["reason"].startswith(kept_reason), d
    assert wt.exists()
    assert not fsmon_watching(wt), "a worktree the retirement gate kept still has its fsmonitor daemon: " + str(d["reason"])


@fsmon_only
@pytest.mark.parametrize("extension", [True, False], ids=["disable-took", "disable-skipped"])
def test_retire_records_each_fsmonitor_step_and_gates_the_final_stop_on_the_disable(fsmon_board, extension):
    """FSMONITOR-STOP-RESULT-VISIBILITY-1 (H1): the disposition carries what the disable and each stop did instead of discarding it.
    RETIRE-FSMONITOR-STOP-COST-1 (H2): the finally stop repeats the early one only when the disable did not take."""
    main, wt = fsmon_board
    if extension:
        git(main, "config", "extensions.worktreeConfig", "true")
    (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    git(wt, "status", "--porcelain")
    assert fsmon_watching(wt), "the fixture failed to start the daemon it is about to measure"
    d = fsmon_retire(wt)
    assert d["action"] == "kept" and d["reason"].startswith("dirty"), d
    step = d["fsmonitorStop"]
    assert set(step) == {"disable", "early", "remove", "final"}, step
    assert step["early"] == "stopped", step
    assert step["remove"] is None, "a kept worktree never reaches the removal stop"
    if extension:
        assert step["disable"] == "disabled" and step["final"] == "not-run: disable succeeded", step
    else:
        assert step["disable"].startswith("skipped: extensions.worktreeConfig"), step
        assert step["final"] == "stopped", "the gate's own status restarted the daemon, so the final stop has to run and say so: " + str(step)
    assert not fsmon_watching(wt)


FSMON_STUB_DISABLE_TOOK = (
    "function Disable-LaneWorktreeFsmonitor { param($WorkDir) [pscustomobject]@{ action = 'disabled'; reason = $null } }"
)


def fsmon_retire_with_stopped_daemon_stub(wt, stop_outcomes):
    """Run the real Invoke-RetireLaneWorktree with the fsmonitor helpers replaced AFTER the library loads: the disable always takes, and the Nth
    stop call returns stop_outcomes[N-1] (the last repeats). Returns (disposition, number of stop calls). No daemon is involved, so the failed-stop
    path that cannot be provoked with a real daemon becomes deterministic."""
    outcomes = ",".join("'%s'" % o for o in stop_outcomes)
    stubs = (
        FSMON_STUB_DISABLE_TOOK + "; $global:StopCalls = 0; $global:Outcomes = @(" + outcomes + "); "
        "function Stop-LaneWorktreeFsmonitor { param($WorkDir) $i = [math]::Min($global:StopCalls, $global:Outcomes.Count - 1); $global:StopCalls++; "
        "if ($global:Outcomes[$i] -eq 'failed') { [pscustomobject]@{ action = 'failed'; reason = 'fsmonitor--daemon stop failed: stub' } } "
        "else { [pscustomobject]@{ action = $global:Outcomes[$i]; reason = $null } } }"
    )
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{FSMON_RETIRE_LIB}'; {stubs}; $snap = {FSMON_NO_PROCS}; "
        f"$d = Invoke-RetireLaneWorktree -WorkDir '{wt}' -MergeTarget 'master' -ProcessSnapshot $snap; "
        "[pscustomobject]@{ d = $d; stopCalls = $global:StopCalls } | ConvertTo-Json -Depth 6"
    )
    done = subprocess.run([FSMON_PWSH, "-NoProfile", "-NonInteractive", "-Command", script], text=True, capture_output=True)
    assert done.returncode == 0, (done.stdout, done.stderr)
    out = json.loads(done.stdout)
    return out["d"], out["stopCalls"]


@fsmon_only
def test_retire_retries_a_failed_early_stop_once_when_the_disable_took(fsmon_board):
    """LANE-FSMONITOR-FOLLOWUPS-1 (b): the early stop failed (a daemon may still run) but the disable took, so the finally block used to write
    'not-run: disable succeeded' and never tried again. The disable only prevents a NEW daemon; it does not end one that is already running."""
    main, wt = fsmon_board
    (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    d, calls = fsmon_retire_with_stopped_daemon_stub(wt, ["failed", "stopped"])
    assert d["action"] == "kept" and d["reason"].startswith("dirty"), "the retry must not change the disposition: " + str(d)
    step = d["fsmonitorStop"]
    assert step["disable"] == "disabled" and step["early"].startswith("failed:"), step
    assert step["final"] == "stopped", "a failed early stop after a successful disable must be retried and the retry recorded: " + str(step)
    assert calls == 2, "exactly one retry"


@fsmon_only
def test_retire_retry_of_a_failed_early_stop_is_bounded_and_records_its_failure(fsmon_board):
    main, wt = fsmon_board
    (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    d, calls = fsmon_retire_with_stopped_daemon_stub(wt, ["failed"])
    assert d["action"] == "kept" and d["reason"].startswith("dirty"), d
    step = d["fsmonitorStop"]
    assert step["early"].startswith("failed:") and step["final"].startswith("failed:"), step
    assert calls == 2, "one retry, never a loop"


@fsmon_only
def test_retire_does_not_retry_when_the_early_stop_did_not_fail(fsmon_board):
    main, wt = fsmon_board
    (wt / "someone-elses-work.txt").write_text("uncommitted\n")
    d, calls = fsmon_retire_with_stopped_daemon_stub(wt, ["stopped"])
    step = d["fsmonitorStop"]
    assert step["early"] == "stopped" and step["final"] == "not-run: disable succeeded", step
    assert calls == 1


def test_fsmonitor_retire_never_discards_a_stop_result_and_gates_the_finally_stop():
    """Source pin for the paths the fixture cannot reach hermetically (a real retirement depends on the host's live processes): the removal stop is
    recorded before `worktree remove`, no stop result is thrown away with `$null =`, and the finally stop is conditional on the disable."""
    retire = (ROOT / "tools" / "coordination" / "Retire-LaneWorktree.ps1").read_text(encoding="utf-8")
    body = retire[retire.index("function Invoke-RetireLaneWorktree"):retire.index("function Invoke-SweepMergedLaneWorktrees")]
    assert "$null = Stop-LaneWorktreeFsmonitor" not in body and "$null = Disable-LaneWorktreeFsmonitor" not in body
    assert body.index("$d.fsmonitorStop.remove =") < body.index("'worktree', 'remove'")
    finally_block = body[body.index("} finally {"):]
    assert "$d.fsmonitorStop.disable -eq 'disabled'" in finally_block and "not-run: disable succeeded" in finally_block
    assert finally_block.index("not-run: disable succeeded") < finally_block.index("Stop-LaneWorktreeFsmonitor -WorkDir $wd")


def test_invoke_lane_stops_the_daemon_when_the_lane_ends_with_the_worktree_kept():
    """A relaunched worktree without extensions.worktreeConfig cannot be disabled, so the lane's own status restarts the daemon after the launch-time
    stop; with no -RetireWorktree nothing else ends it. r2 (sol r1 blocker): the stop has to come AFTER the last git call that touches the worktree
    (the post-exit rev-parse and tracked-dirt status), not merely after the retire and sweep blocks, and before the receipt is built."""
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    tail = lane[lane.index("Invoke-SweepMergedLaneWorktrees -RepoRoot"):]
    assert "Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir" in tail, "Invoke-Lane.ps1 never stops the worktree's daemon at lane end"
    assert not fsmon_end_stop_violation(lane), "a git command runs in Invoke-Lane.ps1 after the lane-end daemon stop"
    stop = lane.rfind(FSMON_END_STOP)
    assert lane.index("$dirtyCheckReason = $null") < stop < lane.index("$receipt = [ordered]@{"), "the lane-end stop must sit after the post-exit dirty check and before the receipt"
    # The stop result is recorded, not discarded (FSMONITOR-STOP-RESULT-VISIBILITY-1).
    assert "fsmonitorStop = $fsmonitorStop" in lane and lane.count("$null = Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir") == 1, "only the launch-time stop may discard its result"


def test_invoke_lane_closes_its_kill_on_close_job_before_the_lane_end_stop():
    """LANE-FSMONITOR-FOLLOWUPS-1 (c), evidence pin (no behaviour change). For a CLAUDE-engine lane, a descendant cannot restart fsmonitor between the
    job close and the lane-end stop because the job's only limit is KILL_ON_JOB_CLOSE (0x2000): no BREAKAWAY_OK (0x800) or SILENT_BREAKAWAY_OK
    (0x1000), so no descendant can leave it, and closing the handle kills every one. The first close sits at the top of the finally block, ahead of
    the retire gate, the sweep, the post-exit dirty check and the lane-end stop. What this does NOT cover: (1) a Codex-engine lane, which is started
    without a job object (the job exists only on the Claude branch), so this ordering says nothing about its descendants; (2) a process started
    OUTSIDE the job (WMI, a scheduled task, COM). Both are the open question in LANE-FSMONITOR-DESCENDANT-RESTART-1."""
    lane = FSMON_LANE_TEXT
    assert "value.BasicLimitInformation.LimitFlags=0x2000;" in lane, "the job limit changed; re-derive whether a descendant can now break away"
    finally_at = lane.index("# Cleanup must precede all receipt construction and I/O")
    first_close = lane.index("[MlvLaneJob]::CloseHandle($jobHandle)", finally_at)
    assert first_close - finally_at < 400, "the job close is no longer the first act of the finally block"
    stop = lane.rfind(FSMON_END_STOP)
    assert first_close < lane.index("Invoke-RetireLaneWorktree -WorkDir $WorkDir") < stop
    assert first_close < lane.index("Invoke-SweepMergedLaneWorktrees -RepoRoot") < stop


@pytest.mark.parametrize(
    "late_git",
    [
        "$late = Get-TrackedDirtyContentIdentity -WorkDir $WorkDir",
        "$late = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('status')",
        "& git -C $WorkDir status --porcelain",
        "$late = git -C $WorkDir status",
    ],
    ids=["tracked-dirt-helper", "capture-helper", "amp-git", "assigned-bare-git"],
)
def test_fsmonitor_end_stop_pin_catches_a_git_call_placed_after_the_stop(late_git):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    anchor = "$receipt = [ordered]@{"
    assert anchor in lane, "the receipt anchor moved; re-pin this test"
    assert fsmon_end_stop_violation(lane.replace(anchor, late_git + "\n" + anchor, 1)), "the pin passes a git call after the lane-end stop: " + late_git


def test_fsmonitor_end_stop_pin_catches_the_r1_order():
    """The r1 head (bcc587c5) stopped the daemon before `$processEnded =` and ahead of the post-exit status. Rebuild that order from the shipped text."""
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    stop_block = lane[lane.index("$fsmonitorStop = $null"):lane.index("# LANE-NO-BACKGROUND-END-TURN-1 round 7 (sol major 3)")]
    moved = lane.replace(stop_block, "")
    moved = moved.replace("$processEnded = ($exitCode -ne -999)", "try { $null = Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir } catch { }\n$processEnded = ($exitCode -ne -999)", 1)
    assert "try { $null = Stop-LaneWorktreeFsmonitor" in moved
    assert fsmon_end_stop_violation(moved), "the pin accepts the r1 order: stop first, then the post-exit status restarts the daemon"


# FSMONITOR-LANE-PIN-BARE-GIT-1: the order pin above looks for ONE git shape (`$x = Invoke-GitCaptureUtf8`), so a bare `& git ...`, `git status`, an
# unassigned or nested Invoke-GitCaptureUtf8, a helper that wraps it, or Start-Process git placed BEFORE the disable would pass. This helper is
# the pin's decision: does the text run a git command ahead of the disable?
FSMON_DISABLE_LINE = "$null = Disable-LaneWorktreeFsmonitor -WorkDir $WorkDir"
FSMON_INSERT_BEFORE = "try {\n    . (Join-Path $PSScriptRoot 'lane-fsmonitor.ps1')"


# Every way Invoke-Lane.ps1 starts git: `& git`, `& git.exe`, `& $gitExe`, a bare `git ...` command, Start-Process, cmd /c, Invoke-Expression, a
# ProcessStartInfo whose FileName is git, Process.Start('git'). A helper that wraps one of these (Invoke-GitCaptureUtf8, and whatever calls it) is git
# too, found by name below, so a call through a helper counts like the raw shape.
FSMON_GIT_SHAPES = (
    r"&[ \t]*['\"]?[^\s'\"|;)]*\bgit(?:\.exe)?['\"]?(?=[\s;)|]|$)",
    r"&[ \t]*(['\"])[^'\"\n]*\bgit(?:\.exe)?\1",  # & 'C:\Program Files\Git\cmd\git.exe' (a quoted path may hold spaces)
    r"&[ \t]*\$\{?\w*git\w*\b",
    r"(?:^|[;{(|=])[ \t]*git(?:\.exe)?[ \t]+[-\w@$]",  # git -C x | git @splat | git $argv | git $args
    r"\bStart-Process\b[^\n]*\bgit\b",
    r"\bStart-Process\b[ \t]+(?:-FilePath[ \t]+)?\$\w*git\w*\b",  # Start-Process $gitExe 'status'
    r"\bStart-Process\b[^\n]*-FilePath[ \t]+\$\w*git\w*\b",
    r"\b(?:cmd|cmd\.exe)[ \t]+/c\b[^\n]*\bgit\b",
    r"\b(?:Invoke-Expression|iex)\b[^\n]*\bgit\b",
    r"\bProcess\]::Start\([ \t]*['\"][^'\"]*git",
    r"\.FileName[ \t]*=[ \t]*['\"][^'\"]*\bgit(?:\.exe)?['\"]",
    r"&[ \t]*\$?\([^)\n]*\bgit\b",  # & (Get-Command git) -C ...   and   & $(Get-Command git) -C ...
)
# A bare `git ...` starts a statement after ; { ( | or `=` (`$out = git status`). Matching is case-insensitive: PowerShell is.
FSMON_GIT_RE = re.compile("|".join("(?:%s)" % s for s in FSMON_GIT_SHAPES), re.M | re.I)
# `function` may be indented (nested in a try/if) and its opening brace may be on the next line. Group 1 is the indent (the closing brace sits at
# the same indent), group 2 the name.
FSMON_FUNCTION_RE = re.compile(
    r"^([ \t]*)function[ \t]+([\w-]+)\b[^\n]*?\n?[ \t]*(?:\{[ \t]*\r?\n.*?^\1\}[ \t]*\r?$|\{[^\n]*\}[ \t]*\r?$)", re.M | re.S | re.I
)
FSMON_STATEMENT_PREFIX = ";{(|= \t\r\n"

# LANE-FSMONITOR-FOLLOWUPS-1 (a): names that STAND FOR git, found by reading the script's own assignments. `$tool = 'git'` / `$g = (Get-Command git).Source`
# make `& $tool ...` and `Start-Process $tool` git calls whatever the variable is called, and `Set-Alias g git` makes a bare `g ...` one. A name
# that is computed, read from the environment or returned by a helper is out of reach of a lexical scan (see FSMON_INDIRECT_KNOWN_GAPS), and so is a
# name that is reassigned to git after a non-git value, or held in a scope-qualified variable ($script:g, $global:g) or a `-Scope`d alias. The
# discovery stays syntax-limited on purpose (LANE-FSMONITOR-FOLLOWUPS-2): those shapes are pinned as misses, not chased.
FSMON_GIT_VALUE = (
    r"(?:['\"](?:[^'\"\n]*[\\/])?git(?:\.exe)?['\"]"
    r"|\(?[ \t]*Get-Command[ \t]+(?:-Name[ \t]+)?['\"]?(?:[^\s'\"|)]*[\\/])?git(?:\.exe)?(?![\w.-]))"
)
FSMON_GIT_VAR_RE = re.compile(r"\$(\w+)[ \t]*=[ \t]*" + FSMON_GIT_VALUE, re.I)
FSMON_GIT_ALIAS_RE = re.compile(
    r"\b(?:Set|New)-Alias\b[ \t]+(?:-Name[ \t]+)?(\w+)[ \t]+(?:-Value[ \t]+)?['\"]?(?:[^\s'\"]*[\\/])?git(?:\.exe)?['\"]?(?=\s|$)", re.I
)


def fsmon_alias_git_re(code):
    """Regex matching a call through a variable or alias that stands for git, or None when the script defines none."""
    variables = sorted({m.group(1) for m in FSMON_GIT_VAR_RE.finditer(code)})
    aliases = sorted({m.group(1) for m in FSMON_GIT_ALIAS_RE.finditer(code)})
    parts = []
    if variables:
        names = "|".join(re.escape(v) for v in variables)
        parts += [
            r"&[ \t]*\$\{?(?:%s)\}?(?![\w])" % names,
            r"\bStart-Process\b[ \t]+(?:-FilePath[ \t]+)?\$(?:%s)(?![\w])" % names,
            r"\bStart-Process\b[^\n]*-FilePath[ \t]+\$(?:%s)(?![\w])" % names,
        ]
    if aliases:
        names = "|".join(re.escape(a) for a in aliases)
        parts += [r"(?:^|[;{(|=])[ \t]*(?:%s)(?=[ \t]|$)" % names, r"&[ \t]*(?:%s)(?![\w-])" % names]
    return re.compile("|".join("(?:%s)" % p for p in parts), re.M | re.I) if parts else None


def fsmon_blank(text, pattern, flags=0):
    """Replace every match with spaces, keeping newlines, so offsets in the result are offsets in the original."""
    return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=flags)


def fsmon_code_mask(text):
    """Per-character flags, True where the character is PowerShell code and False inside a quoted string or a # comment (a trailing comment on a
    code line included). Returns (flags, ended_in_code); a False second value means the lexer lost sync. Block comments and here-strings are
    blanked by the caller first."""
    flags = [True] * len(text)
    state = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if state is None:
            if ch == "#" and (i == 0 or text[i - 1] in " \t\r\n;{()}"):
                end = text.find("\n", i)
                end = n if end < 0 else end
                for k in range(i, end):
                    flags[k] = False
                i = end
                continue
            if ch in "'\"":
                state = ch
                flags[i] = False
        else:
            flags[i] = False
            if state == '"' and ch == "`":
                if i + 1 < n:
                    flags[i + 1] = False
                i += 2
                continue
            if ch == state:
                state = None
        i += 1
    return flags, state is None


def fsmon_git_invocation_offsets(lane_text):
    """Offsets of the top-level (outside any function body) places in a PowerShell script that start git, directly or through a helper. A match
    counts only when its first token is code: a git mention in a quoted string or after a # comment marker does not."""
    code = fsmon_blank(lane_text, r"<#.*?#>", re.S)
    code = fsmon_blank(code, r"@(['\"])[ \t]*\r?\n.*?\n\1@", re.S)
    flags, _ = fsmon_code_mask(code)

    def live(match, text):
        i = match.start()
        while i < match.end() and text[i] in FSMON_STATEMENT_PREFIX:
            i += 1
        return i if i < len(flags) and flags[i] else None

    def mention(name):
        return re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(name), re.I)

    alias_re = fsmon_alias_git_re(code)

    def git_matches(text, lo=0, hi=None):
        hi = len(text) if hi is None else hi
        for rx in (FSMON_GIT_RE, alias_re):
            if rx is not None:
                yield from rx.finditer(text, lo, hi)

    spans = {m.group(2).lower(): (m.start(), m.end()) for m in FSMON_FUNCTION_RE.finditer(code)}
    wrappers = {n for n, (lo, hi) in spans.items() if any(live(m, code) is not None for m in git_matches(code, lo, hi))}
    while True:
        more = {
            n for n, (lo, hi) in spans.items()
            if n not in wrappers and any(live(m, code) is not None for w in wrappers for m in mention(w).finditer(code, lo, hi))
        }
        if not more:
            break
        wrappers |= more
    top = fsmon_blank(code, FSMON_FUNCTION_RE.pattern, FSMON_FUNCTION_RE.flags)
    hits = [live(m, top) for m in git_matches(top)]
    for w in wrappers:
        hits += [live(m, top) for m in mention(w).finditer(top)]
    return sorted(h for h in hits if h is not None)


def fsmon_lane_pin_violation(lane_text):
    disable = lane_text.index(FSMON_DISABLE_LINE)
    hits = fsmon_git_invocation_offsets(lane_text)
    return not hits or hits[0] < disable


FSMON_END_STOP = "Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir"


def fsmon_end_stop_violation(lane_text):
    """True when the lane-end stop is missing or any top-level git call comes after it (a later `git status` restarts the daemon it just stopped)."""
    stop = lane_text.rfind(FSMON_END_STOP)
    hits = fsmon_git_invocation_offsets(lane_text)
    return stop < 0 or stop <= lane_text.index(FSMON_DISABLE_LINE) or bool(hits and hits[-1] > stop)


def test_fsmonitor_lane_pin_accepts_the_shipped_invoke_lane():
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    assert not fsmon_lane_pin_violation(lane)


@pytest.mark.parametrize(
    "git_call",
    [
        "$null = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('status')",
        "& git -C $WorkDir status --porcelain",
        "& git.exe -C $WorkDir status --porcelain",
        "git -C $WorkDir status --porcelain",
        "Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('status')",
        "if ((Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('status')).ok) { }",
        "$dirt = Get-TrackedDirtyContentIdentity $WorkDir",
        "Start-Process -FilePath git -ArgumentList 'status' -Wait",
        "& $gitExe -C $WorkDir status",
        "[void][System.Diagnostics.Process]::Start('git', 'status')",
    ],
    ids=["assigned-helper", "amp-git", "amp-git-exe", "bare-git", "unassigned-helper", "nested-helper", "wrapper-helper", "start-process", "amp-variable", "process-start"],
)
def test_fsmonitor_lane_pin_catches_a_git_command_inserted_before_the_disable(git_call):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    assert FSMON_INSERT_BEFORE in lane, "the insertion anchor moved; re-pin this test"
    mutated = lane.replace(FSMON_INSERT_BEFORE, git_call + "\n" + FSMON_INSERT_BEFORE, 1)
    assert fsmon_lane_pin_violation(mutated), "the pin passes a lane script that runs git before the disable: " + git_call


# FSMONITOR-PIN-POWERSHELL-LEXING-1 / FSMONITOR-LANE-PIN-ASSIGNED-BARE-GIT-1 (r2): the scanner has to read PowerShell the way PowerShell does.
@pytest.mark.parametrize(
    "git_call",
    [
        "$out = git -C $WorkDir status --porcelain",
        "$out = (git -C $WorkDir status)",
        "& (Get-Command git) -C $WorkDir status",
        "& (Get-Command git.exe -ErrorAction Stop).Source -C $WorkDir status",
        "invoke-gitcaptureutf8 -WorkDir $WorkDir -GitArgs @('status')",
        "INVOKE-GITCAPTUREUTF8 -WorkDir $WorkDir -GitArgs @('status')",
        "$dirt = get-trackeddirtycontentidentity $WorkDir",
        "GIT -C $WorkDir status",
        "$x = 'a' # note\n& git -C $WorkDir status",
        "Write-Host \"it's\"; & git -C $WorkDir status",
    ],
    ids=[
        "assigned-bare-git", "paren-bare-git", "amp-get-command", "amp-get-command-exe", "lowercase-helper", "uppercase-helper", "lowercase-wrapper",
        "uppercase-bare-git", "code-after-trailing-comment", "code-after-apostrophe-string",
    ],
)
def test_fsmonitor_lane_pin_reads_powershell_lexing_shapes(git_call):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    mutated = lane.replace(FSMON_INSERT_BEFORE, git_call + "\n" + FSMON_INSERT_BEFORE, 1)
    assert fsmon_lane_pin_violation(mutated), "the pin passes a lane script that runs git before the disable: " + git_call


# LANE-FSMONITOR-FOLLOWUPS-1 (a): the scanner is a regex over PowerShell text, so it is syntax-limited. These pin the indirect and aliased spellings of
# "run git" it must catch (each one a probed shape, see FSMON_INDIRECT_CAUGHT) and name the ones it cannot (FSMON_INDIRECT_KNOWN_GAPS). Detection
# is NOT claimed exhaustive: a name that is computed at run time or read from the environment hides git from any lexical scan.
# 30 entries: 13 already caught before LANE-FSMONITOR-FOLLOWUPS-1 plus 17 it added (the #524 commit message says 31; the table holds 30, pinned below).
FSMON_INDIRECT_CAUGHT = {
    # already caught before this card; pinned so a later regex edit cannot drop them
    "var-assigned-from-get-command": "$git = (Get-Command git.exe).Source\n& $git -C $WorkDir status",
    "amp-fullpath-unquoted": "& C:\\Git\\cmd\\git.exe -C $WorkDir status",
    "amp-fullpath-dq-env": "& \"$env:ProgramFiles\\Git\\cmd\\git.exe\" -C $WorkDir status",
    "start-process-fullpath": "Start-Process -FilePath 'C:\\Program Files\\Git\\cmd\\git.exe' -ArgumentList 'status'",
    "splat-amp-git": "$a = @('-C', $WorkDir, 'status')\n& git @a",
    "splat-amp-var-gitexe": "$a = @('-C', $WorkDir, 'status')\n& $gitExe @a",
    "amp-var-named-for-git-path": "$gitPath = 'C:\\Git\\cmd\\git.exe'\n& $gitPath status",
    "bare-git-with-splat-after-flag": "git -C $WorkDir @a",
    "scriptblock-amp": "& { git -C $WorkDir status }",
    "invoke-expression-string": "Invoke-Expression 'git -C $WorkDir status'",
    "cmd-c": "cmd /c git -C $WorkDir status",
    "process-start-info": "$psi = New-Object Diagnostics.ProcessStartInfo\n$psi.FileName = 'git'",
    "process-start-fullpath": "[void][Diagnostics.Process]::Start('C:\\Git\\cmd\\git.exe', 'status')",
    # extended by this card (RED on the previous scanner)
    "amp-fullpath-sq-with-space": "& 'C:\\Program Files\\Git\\cmd\\git.exe' -C $WorkDir status",
    "amp-fullpath-dq-with-space": "& \"C:\\Program Files\\Git\\cmd\\git.exe\" -C $WorkDir status",
    "amp-braced-variable": "& ${gitExe} -C $WorkDir status",
    "amp-subexpression-get-command": "& $(Get-Command git.exe) -C $WorkDir status",
    "start-process-variable-named-for-git": "Start-Process -FilePath $gitExe -ArgumentList 'status'",
    "start-process-positional-variable": "Start-Process $gitExe 'status'",
    "bare-git-splat": "$a = @('-C', $WorkDir, 'status')\ngit @a",
    "bare-git-exe-splat": "$a = @('-C', $WorkDir, 'status')\ngit.exe @a",
    "bare-git-array-variable": "$a = @('status')\ngit $a",
    "bare-git-args-variable": "git $args",
    "iex-alias": "iex 'git -C $WorkDir status'",
    "set-alias-to-git": "Set-Alias g git\ng -C $WorkDir status",
    "new-alias-named-to-git-exe": "New-Alias -Name gg -Value git.exe\ngg status",
    "variable-holding-git-name-called-with-amp": "$tool = 'git'\n& $tool -C $WorkDir status",
    "variable-from-get-command-called-with-amp": "$g = (Get-Command git).Source\n& $g -C $WorkDir status",
    "variable-holding-git-name-splatted": "$tool = 'git'\n$a = @('-C', $WorkDir, 'status')\n& $tool @a",
    "variable-holding-git-name-in-start-process": "$tool = 'git'\nStart-Process $tool 'status'",
}
# Spellings no lexical scan can resolve: the git name is computed, read from the environment or returned by a function. Each is asserted to be a MISS
# so the gap stays visible; when one is closed, this assertion fails and the entry moves to FSMON_INDIRECT_CAUGHT.
FSMON_INDIRECT_KNOWN_GAPS = {
    "computed-name": "& (\"gi\" + \"t\") -C $WorkDir status",
    "environment-supplied-path": "& $env:GIT_EXE -C $WorkDir status",
    "variable-from-helper-result": "$exe = Find-Tool 'vcs'\n& $exe -C $WorkDir status",
    "variable-from-other-variable": "$tool = $global:Vcs\n& $tool -C $WorkDir status",
    # LANE-FSMONITOR-FOLLOWUPS-2: the discovery reads each assignment on its own line and does not follow a name through scopes or reassignment
    "alias-reassigned-to-git-by-command": "New-Alias g ls\nSet-Alias g (Get-Command git)\ng -C $WorkDir status",
    "alias-reassigned-from-environment": "Set-Alias g ls\nSet-Alias g $env:GIT_EXE\ng -C $WorkDir status",
    "variable-reassigned-from-git-named-variable": "$tool = 'ls'\n$tool = $gitExe\n& $tool -C $WorkDir status",
    "script-scoped-variable": "$script:g = 'git'\n& $script:g -C $WorkDir status",
    "global-scoped-variable-from-get-command": "$global:tool = (Get-Command git).Source\n& $global:tool -C $WorkDir status",
    "alias-with-scope-before-name": "Set-Alias -Scope Global -Name g -Value git\ng -C $WorkDir status",
}
# The opposite direction: the discovery also reads an alias or variable out of a `#` line comment, so a COMMENTED-OUT assignment makes the later call
# look like git. That over-detects (a lane script gets refused a pin it would pass), which is the safe side; pinned so nobody mistakes it for a miss.
# A <# block comment #> is blanked before discovery and is not read (see the unrelated-commands test).
FSMON_INDIRECT_OVERREADS = {
    "commented-out-alias": "# Set-Alias g git\ng -C $WorkDir status",
    "commented-out-variable": "# $tool = 'git'\n& $tool -C $WorkDir status",
    "reassigned-away-from-git": "$tool = 'git'\n$tool = 'ls'\n& $tool -C $WorkDir",
}
FSMON_LANE_TEXT =(ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("git_call", list(FSMON_INDIRECT_CAUGHT.values()), ids=list(FSMON_INDIRECT_CAUGHT))
def test_fsmonitor_lane_pin_catches_indirect_and_aliased_git_before_the_disable(git_call):
    assert FSMON_INSERT_BEFORE in FSMON_LANE_TEXT, "the insertion anchor moved; re-pin this test"
    mutated = FSMON_LANE_TEXT.replace(FSMON_INSERT_BEFORE, git_call + "\n" + FSMON_INSERT_BEFORE, 1)
    assert fsmon_lane_pin_violation(mutated), "the pin passes a lane script that runs git before the disable: " + git_call


@pytest.mark.parametrize("git_call", list(FSMON_INDIRECT_CAUGHT.values()), ids=list(FSMON_INDIRECT_CAUGHT))
def test_fsmonitor_end_stop_pin_catches_indirect_and_aliased_git_after_the_stop(git_call):
    anchor = "$receipt = [ordered]@{"
    assert anchor in FSMON_LANE_TEXT, "the receipt anchor moved; re-pin this test"
    assert fsmon_end_stop_violation(FSMON_LANE_TEXT.replace(anchor, git_call + "\n" + anchor, 1)), "the pin passes a git call after the lane-end stop: " + git_call


@pytest.mark.parametrize("git_call", list(FSMON_INDIRECT_KNOWN_GAPS.values()), ids=list(FSMON_INDIRECT_KNOWN_GAPS))
def test_fsmonitor_lane_pin_known_gaps_are_misses_on_purpose(git_call):
    """KNOWN GAP, expected miss. A scan of the text cannot see that these start git. If this fails, the scanner learned the shape: move it to
    FSMON_INDIRECT_CAUGHT rather than deleting the case."""
    mutated = FSMON_LANE_TEXT.replace(FSMON_INSERT_BEFORE, git_call + "\n" + FSMON_INSERT_BEFORE, 1)
    assert not fsmon_lane_pin_violation(mutated), "known gap closed; promote it to FSMON_INDIRECT_CAUGHT: " + git_call


@pytest.mark.parametrize("git_call", list(FSMON_INDIRECT_OVERREADS.values()), ids=list(FSMON_INDIRECT_OVERREADS))
def test_fsmonitor_lane_pin_over_reads_a_commented_or_reassigned_away_alias_on_purpose(git_call):
    """KNOWN OVER-DETECTION, safe direction. The discovery is syntax-limited, so it takes a commented-out alias or a variable later reassigned to a
    non-git value for a name that stands for git. If this fails, the discovery got smarter: move the case to the not-git list."""
    mutated = FSMON_LANE_TEXT.replace(FSMON_INSERT_BEFORE, git_call + "\n" + FSMON_INSERT_BEFORE, 1)
    assert fsmon_lane_pin_violation(mutated), "the discovery stopped over-reading; move this case to the unrelated-commands test: " + git_call


def test_fsmonitor_indirect_caught_table_holds_30_shapes():
    """LANE-FSMONITOR-FOLLOWUPS-2 (d): the #524 commit message said 31; the table has 30 (13 earlier + 17 added). Update the comment above the table
    when you add a shape."""
    assert len(FSMON_INDIRECT_CAUGHT) == 30, len(FSMON_INDIRECT_CAUGHT)


@pytest.mark.parametrize(
    "not_git",
    [
        "$tool = 'ls'\n& $tool -C $WorkDir",
        "Set-Alias ll Get-ChildItem\nll $WorkDir",
        "$tool = 'github-cli'\n& $tool status",
        "Start-Process notepad $gitNotes",
        "Write-Host 'git $args'",
        "<# Set-Alias g git #>\ng -C $WorkDir status",
    ],
    ids=["variable-holds-other-tool", "alias-to-other-command", "variable-holds-name-merely-containing-git", "start-process-other-tool", "string-mention", "block-commented-alias"],
)
def test_fsmonitor_lane_pin_indirect_shapes_do_not_flag_unrelated_commands(not_git):
    mutated = FSMON_LANE_TEXT.replace(FSMON_INSERT_BEFORE, not_git + "\n" + FSMON_INSERT_BEFORE, 1)
    assert not fsmon_lane_pin_violation(mutated), "an unrelated command was read as git: " + not_git


# A helper is git too, so a CALL ahead of the disable counts wherever the helper is defined and however it is spelled.
FSMON_HELPER_DEFINITIONS = [
    "if ($true) {\n    function Run-It {\n        & git -C $WorkDir status\n    }\n}",
    "  Function Run-It {\n    & git -C $WorkDir status\n  }",
    "function Run-It\n{\n    & git -C $WorkDir status\n}",
    "FUNCTION Run-It\r\n{\r\n    & git -C $WorkDir status\r\n}",
    "function run-it { & git -C $WorkDir status }",
]
FSMON_HELPER_IDS = ["indented", "indented-capital", "next-line-brace", "next-line-brace-crlf-upper", "one-line-lowercase"]


@pytest.mark.parametrize("definition", FSMON_HELPER_DEFINITIONS, ids=FSMON_HELPER_IDS)
def test_fsmonitor_lane_pin_finds_a_helper_call_before_the_disable_however_the_helper_is_defined(definition):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    mutated = lane.replace(FSMON_INSERT_BEFORE, "Run-It\n" + FSMON_INSERT_BEFORE, 1) + "\n" + definition + "\n"
    assert fsmon_lane_pin_violation(mutated), "the pin passes a call to a git helper before the disable: " + definition


@pytest.mark.parametrize("definition", FSMON_HELPER_DEFINITIONS, ids=FSMON_HELPER_IDS)
def test_fsmonitor_lane_pin_ignores_a_helper_that_is_defined_before_the_disable_but_never_called_there(definition):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    mutated = lane.replace(FSMON_INSERT_BEFORE, definition + "\n" + FSMON_INSERT_BEFORE, 1)
    assert not fsmon_lane_pin_violation(mutated), "a function body that only defines git work was read as a top-level git call: " + definition


@pytest.mark.parametrize(
    "not_git",
    [
        "$x = 1 # & git -C $WorkDir status",
        "$x = 1 # Invoke-GitCaptureUtf8 -WorkDir $WorkDir",
        "Write-Host \"about to run: & git status\"",
        "Write-Host 'then; git status'",
        "Write-Host \"a; git status; b\"",
        "Write-Host \"escaped \\`\" & git status\"",
        "$msg = \"call Invoke-GitCaptureUtf8 later\"",
        "function Run-It {\n    # & git status\n    'git status'\n}\nRun-It",
    ],
    ids=["trailing-comment-amp", "trailing-comment-helper", "dq-string-amp", "sq-string-semicolon", "dq-string-semicolon", "escaped-quote-string", "helper-name-in-string", "comment-and-string-in-function"],
)
def test_fsmonitor_lane_pin_ignores_a_git_mention_in_a_comment_or_a_string(not_git):
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    mutated = lane.replace(FSMON_INSERT_BEFORE, not_git + "\n" + FSMON_INSERT_BEFORE, 1)
    assert not fsmon_lane_pin_violation(mutated), "a comment or string was read as a git call: " + not_git


def test_fsmonitor_lexer_stays_in_sync_over_the_shipped_lane_script():
    lane = (ROOT / "tools" / "coordination" / "Invoke-Lane.ps1").read_text(encoding="utf-8")
    code = fsmon_blank(lane, r"<#.*?#>", re.S)
    code = fsmon_blank(code, r"@(['\"])[ \t]*\n.*?\n\1@", re.S)
    flags, ended_in_code = fsmon_code_mask(code)
    assert ended_in_code, "the lexer ended inside a string: it lost sync with Invoke-Lane.ps1"
    assert not fsmon_lane_pin_violation(lane) and not fsmon_end_stop_violation(lane)
