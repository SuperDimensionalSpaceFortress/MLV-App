"""Falsifier table for the MLV-App project PreToolUse gate (tools/hooks/mlv-never-authorized.py).

WHY EVERY CASE LIVES IN A ``unittest.TestCase`` SUBCLASS, AND WHY THERE ARE NO subTests.
The repo-hygiene job runs ``python -m unittest discover -s tools/repo_hygiene -p "test_*.py"
-t .``.  Under that runner a module-level ``def test_*`` table collects ZERO cases and exits
GREEN (O79) -- the failure mode nobody would notice, in the one suite whose whole job is to
prove enforcement.  And ``unittest`` counts ``Ran N`` once per METHOD, so a subTest table of
thirty rows reports ``Ran 1 test``, which the ``N >= rows + controls`` acceptance reads as RED
(O90).  So the table below is expanded by ``setattr`` into ONE ``def test_*`` METHOD PER ROW.

EVERY PATH IS A TMP-DIR FIXTURE.  The board root (``MLV_BOARD_ROOT``), the clip cache
(``MLV_CLIP_CACHE_ROOT``) and the required-checks snapshot
(``MLV_REQUIRED_CHECKS_SNAPSHOT``) are PARAMETERS this test supplies, never literals the
hook hardcodes -- so the table is green on both matrix legs and no case touches
``.claude-state`` or the real board root, which are gitignored and absent from every hosted
checkout (O81/O97).

THE VENUE IS AN ARGUMENT, NOT A VARIABLE (O126).  Three rules key on it -- NA-2 exception
(iv), the dedicated 0.2 enable act (S105) and NA-10 -- and the hook now reads it from
``--project-dir`` on its own command line, because the environment variable the previous
revision read was measured ABSENT from real hook processes, so that test never fired.  A
row declares its venue through its fixture's ``__project_dir__`` key, which ``_invoke``
turns into an ARGUMENT; ``_invoke`` also STRIPS ``CLAUDE_PROJECT_DIR`` from the environment,
and one row deliberately puts it BACK while passing no argument, so that a hook which
regressed to reading the environment goes red rather than looking correct.

THE ENABLE LITERAL IS DERIVED FROM THE FIXTURE, NOT WRITTEN DOWN (S112, register v21).  The
hook now validates the 0.2 enable compound's JSON literal SEMANTICALLY: ``state`` is
``enabling``, both stamps carry the ONE pinned notation described below,
``executionControlReceipt`` is the BASENAME of the newest valid execution-control receipt,
and ``executionControlSha256`` is that file's lowercase sha256.  Two of those are facts
about a tmp-dir board that does not exist until ``setUp`` runs, so the table carries them
as PLACEHOLDERS (``{NEWEST_CONTROL}``, ``{NEWEST_CONTROL_SHA}`` and the near-miss forms)
which ``_bind_the_enable_literal`` resolves by reading the receipts the fixture just wrote
and hashing their bytes.  Hard-coding either
would leave the ALLOW row green against a hook that never opened the file -- which is exactly
the defect S112 names.  What the table's own defaults used to be says it best: the receipt
was ``receipts/execution-control-0.7.json``, a path, and the digest was ``"3" * 64``.  Both
are DENY rows now.

ONE FIXED NOTATION, AND THE NEWEST RECEIPT IS THE PARSED ONE (O141, register v21).
Every ``recordedUtc`` the hook reads or validates carries exactly
``YYYY-MM-DDTHH:MM:SSZ`` -- whole seconds, uppercase ``Z``, no fraction, no offset -- and
the newest ``execution-control-*.json`` is chosen by the PARSED value, with a
non-conforming stamp or an exact tie making the newest UNDECIDABLE (fail closed, naming the
file, or both files for a tie).  The sixth commit compared the raw strings and accepted an
optional fraction in the literal; the hub reproduced the consequence ON that commit, with
``0.6`` at ``2026-09-06T16:00:00Z`` beside ``0.7`` at ``2026-09-06T16:00:00.500000Z``,
where ``'...:00Z' > '...:00.500000Z'`` is True in Python and the STALE receipt was selected
as newest.  That exact pair is a DENY row below, beside the offset, naive, lowercase-``z``
and tied-stamp rows, the two fractional literal stamps, and an ALLOW/DENY pair on two
whole-second stamps one second apart that pins WHICH receipt the selection returns.
``_pinned_moment`` parses the notation with the harness's OWN implementation, so this
table's idea of "newest" is never derived from the hook it tests.

THE SIX GATE RECEIPTS VALIDATE AGAINST A SCHEMA TABLE (S118, register v21).  Until this
delta "validate" meant ``json.load`` returned something other than ``None``, so ``{}``
passed -- and the hub reproduced the consequence ON the sixth commit: five ``{}`` receipts
beside ``execution-control-forged.json`` carrying only the three provenance keys ALLOWED the
canonical enable, exit 0.  The hook now implements the ONE table of plan 1.3 step 5, and so
does this suite's fixture: every gate receipt is a non-empty object carrying ``recordedUtc``
in the pinned notation, with ``sha256`` = 64 lowercase hex, ``sha`` = 40 lowercase hex,
``path`` = a file that EXISTS (absolute, else relative to the board root), ``url`` = begins
``https://github.com/layibabalola/MLV-App/``, ``int`` = a non-negative integer where a
BOOLEAN is not one, and ``list`` = a JSON array.  The execution-control candidate set is the
SIX chain names ``execution-control-{0.1,0.35,0.4c-i,0.4b-i,0.6,0.7}.json`` and no other; the
SELECTED receipt is ``0.7`` when a valid one exists else ``0.6``, and the parsed-newest valid
chain receipt must BE that one.

EVERY BOUND VALUE IN EVERY FIXTURE IS COMPUTED, NEVER WRITTEN DOWN.  The `path` fields name
files this fixture just wrote under the tmp board; the digests are real digests; and
``roadmapParityReceiptSha256`` is the sha256 of ``0.18-roadmap-parity.json`` READ BACK OFF
DISK after the write, substituted into each chain receipt at write time.  A table carrying a
hard-coded digest would stay green against a hook that had stopped opening the file, which
is the S112/S118 defect itself.

O152, THE STRICT RULE, AND THE ROW THAT MEASURES IT.  A chain receipt that is PRESENT but
invalid is never silently excluded: it makes the newest UNDECIDABLE and the act fails closed
naming it.  The falsifier is a present-but-invalid ``0.7`` (no ``queueSha256``) beside a
VALID ``0.6``, with the enable literal naming ``0.6`` and carrying ``0.6``'s real digest --
so the rejected "exclude" reading would ALLOW that row, selecting a STALE receipt, which is
O141's hazard in a new shape.  The literal names it through ``{CONTROL_0_6}`` /
``{CONTROL_0_6_SHA}``, placeholders ``_bind_the_enable_literal`` publishes for EVERY chain
receipt on disk, valid or not, because ``{NEWEST_CONTROL}`` cannot express "the 0.6
specifically" when the rule under test is what "newest" means.

THE SECOND KEY'S APPROVAL, THE FIXED HASH SET, AND THE RE-HASH (S120/O158/O159, register
v22).  Every chain receipt the fixtures write now carries ``reviewedHeadSha`` (a derived
40-hex head) and ``solVerdictPath`` naming a verdict file the fixture WROTE -- sol's
template shape, prose ending in a ```json fence whose block is ``{"verdict": "APPROVE",
"subject_sha": <that head>, ...}``, with an EARLIER fenced block carrying a BLOCKER so the
ALLOW rows prove the hook reads the TERMINAL block; 0.35's is saved as plain JSON, the
"last top-level object" reading -- and a ``hashes`` object whose KEY SET is EXACTLY the
step's fixed set with the REAL sha256 of each file, which the fixture writes under the tmp
board's ``tools/`` tree so the O158 re-hash has something to walk.  ``REQUIRED_HASHES``
restates plan 1.3 step 5's table in full rather than deriving it, so the hook's cumulative
derivation and this table are two readings the six per-step ALLOW rows force to agree.
The O158 rows mutate the BOARD, not a receipt: one file of the selected receipt's set is
edited after the receipt was written, one is deleted.  The O159 pair rewrites
``0.18-roadmap-parity.json`` non-shrinking and re-points every chain receipt from 0.35 on
(ALLOW), then leaves 0.35 behind (DENY, naming it) -- the hub procedure measured through
the hook's strict rule.

THE TWO SHAS (S123, register v23).  ``reviewedHeadSha`` is the PR HEAD sol reviewed BEFORE
the merge -- the sha the verdict's ``subject_sha`` binds to -- and ``mergeSha`` is the
post-merge commit the receipt's ``hashes`` were taken at.  A GitHub merge lands a DIFFERENT
commit, so the normal post-merge receipt carries TWO different shas and the verdict binds
to the FIRST.  Every chain receipt the fixtures write carries both, DERIVED so that they
differ (``_head_sha`` and ``_merge_sha``), and every verdict is written at the reviewed
head and never at the merge sha -- so a hook that bound the verdict to ``mergeSha`` would
go RED on every ALLOW row, which is the reading S123 exists to rule out.  Four DENY rows
vary ``mergeSha`` alone (absent on the selected 0.7 and on the non-selected 0.6, uppercase,
39 characters); one ALLOW row carries an EXPLICITLY different merge sha; one ALLOW row
carries ``mergeSha == reviewedHeadSha``, because a rebase lands the reviewed commit itself
as the tip and the hook, not knowing the merge method, judges SHAPE only.  The equality of
the fixed set's hashes at the two shas is the HUB's assertion before the receipt is
written, outside the hook, and no row models git.

THE PRE-FLIGHT ARTIFACT, THE PROOF, AND THE CASCADE'S STAMPS (S125/O172/O171, register
v25).  The hub measured on the tenth commit that a ``Write`` of the exact hook-stdin JSON of
the canonical compound to ``$D/receipts/0.2-enable-preflight-input.json`` was ALLOWED by the
receipts carve-out -- and so was the same content to ``notes.json``, under ``fleet-runs/``
and from a worktree venue -- while the register said any other input naming both paths is
DENIED.  S125 makes that Write the SECOND dedicated act, decided BEFORE generic content
attribution: ALLOW only at the board venue, onto an absent target, with content that is
exactly one JSON object whose ``tool_name`` is ``PowerShell`` and whose ``command`` the
enable act would accept RIGHT NOW; every other file-tool payload naming both paths (or the
marker under a delete verb) is DENY wherever it goes.  The S125 rows carry the artifact's
content as a SPEC (``_preflight_content``) that ``_render_content`` JSON-encodes AFTER the
placeholders are substituted, because a Windows tmp path carries backslashes and a
backslash is not valid bare inside a JSON string -- which is also what the hub does: the
artifact is the escaped stdin text.  O172 adds ``fixedSetEqualityProof`` to every chain
receipt -- ``<reviewedHeadSha>=<mergeSha>:<digest>`` -- and the fixtures DERIVE it from the
receipt's own shas and its own ``hashes`` (``_equality_proof`` over ``_fixed_set_digest``,
the suite's own restatement of the canonicalisation: the ``<sha256>  <path>`` lines, sorted,
LF-joined, no trailing newline); five DENY rows vary it one part at a time.  O171 pins that
the O159 cascade preserves each rewritten receipt's ORIGINAL ``recordedUtc``: the ninth
commit's cascade fixture already did, now asserted structurally, and ONE new row restamps
0.6 and 0.7 at the rewrite's instant -- a tie, undecidable.

THE FALSE-POSITIVE PAIRS (HOOK-FALSE-POSITIVE-1, rows i-x; ``falsepositive1``, 30 rows).
Each pair is an ALLOW SUBJECT beside a DENY CONTROL, so a hook that simply stopped
enforcing goes RED on the control rather than looking fixed.

PAIR DISCIPLINE, STATED HONESTLY (round 2, sol's MAJOR on PR #104).  Round 1 claimed every
pair differed in exactly one load-bearing token and only pair vi actually did -- the rest
moved command, tool, path and payload at once, which is a comparison of two situations, not
of one token.  The rows below are rewritten so that (i), (ii), (iv), (v), (vi), (vii) and
(viii) each vary EXACTLY ONE token -- a redirect target, a basename, a directory segment, a
payload, a verb, a root -- with everything else byte-identical.  ONE row is NOT a one-token
pair and says so in its own comment rather than claiming otherwise: (iii), where the
subject is an interpreter heredoc and the control must be a shell truncation verb BECAUSE
the claim is precisely that those are different classes and no single token turns one into
the other.  Do not restore the one-token claim over a pair that does not meet it.

What each pair isolates.  (i)/(ii) a redirect truncates only its TARGET, so
``cat <ledger> 2>/dev/null`` READS while the same command with the ledger as the redirect's
target still writes.  (iii) an interpreter body is still not decoded.  (iv) a ``Write`` to
an ABSENT target creates rather than overwrites -- with the receipt NAME class and the
receipts DIRECTORY class each held back by a one-token control, and the shrink guard proved
by payload alone.  (v) NA-7's roots did NOT widen: a ``Write`` into a SIBLING REPOSITORY is
refused by the ordinary rule, which is the NARROWING pinned as a row rather than left as an
absence.  (vi) a bit bucket is a sink, not a path outside the roots.
(vii) S133: ``..`` is COLLAPSED before every root and prefix comparison -- the round-1 hook
admitted ``<root>/../escaped.txt`` because the root was still a literal prefix of it
(sol's blocker) -- proved under both NA-7 roots AND against an NA-2 protected path,
whose ``..`` detour must still be recognised as the protected file while an unprotected
path's identical detour stays ALLOW.  (viii)/(ix) S134: the names the hook READS are
substituted at the value it is USING -- override when set, built-in default when not -- so a
delete of the protected queue ledger spelled ``$env:MLV_BOARD_ROOT\\...`` is denied in BOTH
configurations and names the RESOLVED path.  (x) a reference the hook CANNOT resolve is not
canonicalised into a relative name and is DENIED as unresolvable, with an ordinary relative
path beside it to prove the new rule is not doing the ``..`` collapse's job.  Every root is
a tmp fixture, so no row depends on this machine's layout.

NOTHING IS EXECUTED BY THE FALSIFIER TABLE.  Every falsifier reaches the hook as a JSON
payload on stdin, delivered to a subprocess started with ``sys.executable``.  The command
strings are data -- including the O129 rows, whose ``setx`` and ``SetEnvironmentVariable``
text never leaves this file.  The ONE exception is ``EnableCompoundIsFailClosedTests``, the
S101/O128 acceptance: the register's fail-closed shape rests on claims about what PowerShell
DOES with a failed ``Set-Content``, and a rule resting on an unverified runtime claim rests
on a guess -- so those five cases run the canonical compound under ``pwsh`` against TEMP
PATHS ONLY (a scratch marker this test created, a receipt under a scratch directory, and one
drive letter that does not exist), never the board, and they SKIP rather than pretend when
``pwsh`` is absent.
"""

import datetime
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO_ROOT, "tools", "hooks", "mlv-never-authorized.py")
# ROUND 3 (S134): this suite's own text, so a gate that depends on a HARNESS line can assert
# that line instead of remembering it.  Read once, at import, from `__file__` itself.
with open(os.path.abspath(__file__), "r", encoding="utf-8") as _handle:
    HARNESS_SOURCE = _handle.read()


def _load_hook_module():
    """O175: the hook's OWN module object, loaded directly -- not the suite's independent
    restatement (``_fixed_set_digest`` below, a SECOND implementation deliberately kept out
    of sync with the hook's import graph) and not a subprocess.

    Safe to import: ``main()`` below reads stdin only under ``if __name__ == "__main__"``
    (see the bottom of the hook file), so loading the module by path runs every module-level
    ``def``/``class``/constant and nothing else -- no I/O, no stdin read.  This is what lets
    an O175 test assert that the hook's canonicalisation FUNCTION, not a copy of its prose,
    returns the pinned worked digest.
    """
    spec = importlib.util.spec_from_file_location(
        "_mlv_never_authorized_hook_under_test", HOOK
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTER_ROWS_WITH_DENY_CASES = (
    "NA-1",
    "NA-2",
    "NA-3",
    "NA-4",
    "NA-6",
    "NA-7",
    "NA-8",
    "NA-9",
    # NA-10 joined the register at v18 (O129, hub extension) and is enforced by this hook,
    # so it joins the gate: a row with zero DENY cases is a rule with no proof.
    "NA-10",
)

APP_ID = 15368
LIVE_CONTEXTS = (
    "Repo Hygiene Python (windows-latest)",
    "Repo Hygiene Python (ubuntu-latest)",
    "Windows GUI Pilot",
    "Windows Product Oracles",
    "Factory Bridge Regressions",
)
CANONICAL_04B_CONTEXTS = (
    "Repo Hygiene Python (windows-latest)",
    "Repo Hygiene Python (ubuntu-latest)",
    "Windows GUI Pilot",
    "Windows Product Oracles",
    "Batch Compile",
)


def _body(contexts, strict=True, app_id=APP_ID):
    return {
        "strict": strict,
        "checks": [{"context": name, "app_id": app_id} for name in contexts],
    }


def _patch(body):
    return (
        "echo '"
        + json.dumps(body)
        + "' | gh api -X PATCH "
        + "repos/layibabalola/MLV-App/branches/master/protection/required_status_checks "
        + "--input -"
    )


ADD_ONLY_PATCH = _patch(_body(tuple(LIVE_CONTEXTS) + ("Batch Compile",)))
CANONICAL_04B_PATCH = _patch(_body(CANONICAL_04B_CONTEXTS))
REMOVAL_PATCH = _patch(_body(tuple(c for c in LIVE_CONTEXTS if c != "Windows GUI Pilot")))

WORKFLOW_BEFORE = (
    "jobs:\n"
    "  product-oracles:\n"
    "    steps:\n"
    "      - name: Batch Compile\n"
    "        run: |\n"
    "          cmake --build build --target batch_compile\n"
    "  hygiene:\n"
    "    steps:\n"
    "      - name: Lint\n"
    "        run: python -m flake8 tools\n"
)
WORKFLOW_MOVED = (
    "jobs:\n"
    "  product-oracles:\n"
    "    steps:\n"
    "      - name: Lint\n"
    "        run: python -m flake8 tools\n"
    "  batch:\n"
    "    steps:\n"
    "      - name: Batch Compile\n"
    "        run: |\n"
    "          cmake --build build --target batch_compile\n"
)
WORKFLOW_DELETED = (
    "jobs:\n"
    "  product-oracles:\n"
    "    steps:\n"
    "      - name: Lint\n"
    "        run: python -m flake8 tools\n"
)

TEST_BEFORE = "void tst::check() {\n    QVERIFY(ok);\n    QCOMPARE(a, b);\n}\n"
TEST_WEAKENED = "void tst::check() {\n    // disabled during triage\n    QCOMPARE(a, b);\n}\n"

RECEIPT_LONG = json.dumps({"note": "x" * 400, "step": "0.4b"}, indent=2)
RECEIPT_SHORT = json.dumps({"step": "0.4b"})
CHECKPOINT_LONG = "# orchestrator resume\n" + ("derived value line\n" * 40)


# ------------------------------------------- the 0.2 enable as ONE dedicated act
#
# The canonical compound is the ONLY input that may delete the marker (S99/O118).  Every
# builder below emits a SINGLE-QUOTE-FREE JSON literal, because the canonical shape's
# literals are single-quoted and the hook refuses a quote of their own (fail-closed).
ENABLE_RECEIPT = "{RECEIPTS}/0.2-loop-enabled.json"
ENABLE_MARKER = "{DUAL}/WORKSTREAM-LOOP-DISABLED"


# S112 -- THE TWO FIELDS THAT BIND THE LITERAL TO THE BOARD ARE PLACEHOLDERS, NEVER
# CONSTANTS.  The hook now requires `executionControlReceipt` to be the BASENAME of the
# fixture's newest execution-control receipt and `executionControlSha256` to be that file's
# real lowercase sha256.  Both are resolved by `_substitute` from the files the fixture
# JUST WROTE (`_enable_literal_bindings`), because a hard-coded name or digest would keep
# this table green against a hook that never opened the file -- which is the S112 defect
# itself.  Note what the old default was: `receipts/execution-control-0.7.json`, a PATH, and
# `"3" * 64`, a digest of nothing.  Both passed presence-only validation; both are now DENY
# rows below.
CONTROL_NAME = "{NEWEST_CONTROL}"
CONTROL_SHA = "{NEWEST_CONTROL_SHA}"


# ------------------------------------------- O141: ONE notation, and the newest is PARSED
#
# Register v21 pins ONE notation for every `recordedUtc` the hook reads or validates:
# `YYYY-MM-DDTHH:MM:SSZ` -- whole seconds, uppercase `Z`, no fraction, no offset.  The
# stamps below are the hub's own round-21 reproduction pair and its near misses.  Under the
# SIXTH commit's raw-string comparison `CONTROL_STAMP_WHOLE > CONTROL_STAMP_FRACTION` is
# TRUE (`Z` is 0x5A, `.` is 0x2E), so the STALE 0.6 was selected as newest and the one-shot
# enable could fire against it; `test_the_o141_stamp_constants_are_what_they_claim` asserts
# that inversion directly, so the defect this delta closes is measured in this file and not
# merely described.
PINNED_UTC_RX = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
PINNED_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONTROL_STAMP_WHOLE = "2026-09-06T16:00:00Z"
CONTROL_STAMP_LATER = "2026-09-06T16:00:01Z"
CONTROL_STAMP_FRACTION = "2026-09-06T16:00:00.500000Z"
CONTROL_STAMP_OFFSET = "2026-09-06T16:00:00+00:00"
CONTROL_STAMP_NAIVE = "2026-09-06T16:00:00"
CONTROL_STAMP_LOWER_Z = "2026-09-06T16:00:00z"
# The four the register refuses, kept as ONE list so a row cannot quietly stop being a near
# miss: every one is a NON-EMPTY STRING that presence-only validation would have accepted.
CONTROL_STAMPS_REFUSED = (
    CONTROL_STAMP_FRACTION,
    CONTROL_STAMP_OFFSET,
    CONTROL_STAMP_NAIVE,
    CONTROL_STAMP_LOWER_Z,
)


def _pinned_moment(stamp):
    """The HARNESS's own parse of the pinned notation -- deliberately a second implementation.

    The table's expectation of which receipt is newest must not be derived from the hook it
    is testing, so this does not import the hook: it re-states the notation and parses it.
    A stamp the hook would refuse returns ``None`` here too, and the caller skips it for
    BINDING purposes only -- the hook fails the whole exception closed on one.
    """
    if not isinstance(stamp, str) or PINNED_UTC_RX.match(stamp) is None:
        return None
    try:
        return datetime.datetime.strptime(stamp, PINNED_UTC_FORMAT)
    except ValueError:
        return None


def _enable_literal(**overrides):
    document = {
        "state": "enabling",
        "enabledUtc": "2026-09-06T12:00:00Z",
        "executionControlReceipt": CONTROL_NAME,
        "executionControlSha256": CONTROL_SHA,
        "recordedUtc": "2026-09-06T12:00:00Z",
    }
    document.update(overrides)
    for key in [key for key, value in document.items() if value is None]:
        del document[key]
    literal = json.dumps(document)
    assert "'" not in literal, literal
    return literal


ENABLE_LITERAL = _enable_literal()

# The pwsh acceptance below never reaches the hook and never opens a fixture board, so its
# literal is CONCRETE: the placeholders exist to bind the HOOK's semantic check to a fixture,
# and `EnableCompoundIsFailClosedTests` measures PowerShell's runtime instead.  An
# unsubstituted `{NEWEST_CONTROL}` would still round-trip byte-for-byte and prove the same
# thing, but it would read as a bug to the next person.
RUNTIME_ENABLE_LITERAL = _enable_literal(
    executionControlReceipt="execution-control-0.7.json",
    executionControlSha256="3" * 64,
)


ENABLE_THROW = "enable-receipt-write-verification-failed"
WRITE_TAIL = " -NoNewline -Encoding utf8 -ErrorAction Stop"
DELETE_TAIL = " -ErrorAction Stop"
ENABLE_PREFERENCE = "Stop"


def _canonical_enable(
    literal=ENABLE_LITERAL,
    receipt=ENABLE_RECEIPT,
    marker=ENABLE_MARKER,
    readback=None,
    throw=ENABLE_THROW,
    write_tail=WRITE_TAIL,
    delete_tail=DELETE_TAIL,
    verify=True,
    preference=ENABLE_PREFERENCE,
):
    """The register's canonical FAIL-CLOSED compound, verbatim (S101/O128).

    Every keyword is ONE degree of freedom of the pinned shape, so a row that varies it
    differs from the ALLOW row in exactly that variable and nothing else.
    ``preference=None`` drops the LEADING ``$ErrorActionPreference = 'Stop'`` and so
    reproduces the REV-19 shape; adding ``verify=False`` and empty tails to that reproduces
    the REV-18 shape.  Both WERE canonical, one and two revisions ago; neither is
    grandfathered.
    """
    readback = receipt if readback is None else readback
    text = ""
    if preference is not None:
        text += "$ErrorActionPreference = '" + preference + "'; "
    text += (
        "$r = '" + literal + "'; "
        "Set-Content -LiteralPath '" + receipt + "' -Value $r" + write_tail + "; "
    )
    if verify:
        text += (
            "if ((Get-Content -LiteralPath '" + readback + "' -Raw -ErrorAction Stop) "
            "-cne $r) { throw '" + throw + "' }; "
        )
    text += "Remove-Item -LiteralPath '" + marker + "'" + delete_tail
    return text


def _rev18_enable():
    """The pre-S101 shape: a NON-terminating Set-Content and no read-back verification."""
    return _canonical_enable(
        write_tail="", delete_tail="", verify=False, preference=None
    )


def _rev19_enable():
    """The pre-O128 shape: every parameter-level arm, but NO leading preference statement.

    The hub measured this form deleting the marker and writing nothing when the receipt
    path's drive does not resolve: the provider dynamic parameters never bind, and
    ``-ErrorAction`` goes unbound with them.  A preference variable cannot come unbound,
    which is why the leading statement -- and not the parameter -- is what closes that class.
    """
    return _canonical_enable(preference=None)


# ------------------------------------------- S125: the PRE-FLIGHT ARTIFACT, the second act
#
# Before typing the enable the hub writes the EXACT hook-stdin JSON of the compound to this
# receipt path with the `Write` tool and runs the live hook on it under MLV_HOOK_DRYRUN=1.
# That Write is the SECOND dedicated act; every other file-tool payload carrying the same
# content is refused wherever it goes.
PREFLIGHT_ARTIFACT = "{RECEIPTS}/0.2-enable-preflight-input.json"
PREFLIGHT_CONTENT = "__preflight_content__"


def _preflight_content(command=None, tool_name="PowerShell", shape="one"):
    """The hook-stdin JSON the hub writes before typing the enable, as a SPEC rendered at run time.

    The content embeds the compound, whose paths are tmp-board placeholders that
    `_substitute` resolves -- and a Windows tmp path carries backslashes, which are not
    valid bare inside a JSON string.  So the row carries the compound UNRENDERED and
    `_render_content` JSON-encodes it AFTER substitution, which is also what the hub does:
    the artifact is the exact stdin text, escapes included.  `shape` renders the near
    misses: `two` concatenates the object with itself, `not-json` drops the closing brace.
    """
    return {
        PREFLIGHT_CONTENT: {
            "command": _canonical_enable() if command is None else command,
            "tool_name": tool_name,
            "shape": shape,
        }
    }


# ------------------------------------------- the four ratified manifest surfaces (O124)
#
# `$D/DEFINITIVE-FIX-PLAN-*.md` is a basename glob on a DIRECT CHILD of `$D`;
# `prompts/v2/**` is a prefix.  The fixture writes each surface with MANIFEST_EXISTING, so
# a LONGER and a SHORTER write are both meaningful against a target that exists.
MANIFEST_PLAN = "{DUAL}/DEFINITIVE-FIX-PLAN-20260906.md"
MANIFEST_REGISTER = "{DUAL}/never-authorized.json"
MANIFEST_WRAPPER = "{DUAL}/Start-EditingLane.ps1"
MANIFEST_PROMPT = "{DUAL}/prompts/v2/card-TOOL-HOOK-ENFORCE-1.md"
MANIFEST_SURFACES = (MANIFEST_PLAN, MANIFEST_REGISTER, MANIFEST_WRAPPER, MANIFEST_PROMPT)

MANIFEST_EXISTING = "# ratified manifest surface\n" + ("pinned line\n" * 30)
MANIFEST_LONGER = MANIFEST_EXISTING + ("amended line\n" * 10)
MANIFEST_SHORTER = "# ratified manifest surface\n"


def _set_content(path):
    """A shell TRUNCATING write -- denied on a protected path unless exception (iv) opens."""
    return "Set-Content -LiteralPath '" + path + "' -Value 'amended by re-ratification'"


# ------------------------------------------- NA-3 / O129: PERSISTENT-scope hook inputs
#
# The six names are the hook's OWN inputs, and a persistent write of any of them is
# inherited by every later hook process -- it does not break a rule, it MOVES the ground the
# rules stand on.  The payload text below is DATA: it is delivered to the hook on stdin and
# never reaches a shell, here or anywhere else in this repo.
#
# This tuple is not decorative: `test_every_environment_input_the_hook_reads_is_na3_protected`
# derives the real list from the hook's own `env.get(...)` calls and asserts it against the
# hook's regex, so a steering input cannot ship unprotected.  The recorded reason is a root
# that once did exactly that -- it widened NA-7 and was absent from this list, so it could be
# `setx`-ed to `C:\` -- and that root has since been removed from the gate entirely.
PERSISTENT_NAMES = (
    "CLAUDE_PROJECT_DIR",
    "MLV_BOARD_ROOT",
    "MLV_LANE_PROMPT",
    "MLV_REQUIRED_CHECKS_SNAPSHOT",
    "MLV_CLIP_CACHE_ROOT",
)
# Assembled from fragments so the persistent-write verbs never appear as one literal token
# in this source either -- the machine-level continuity gate matches TEXT, and a maintainer
# grepping this file should not have to think about whether their pager is a shell.
_PERSIST_VERB = "set" + "x"
_PERSIST_API = "Set" + "EnvironmentVariable"


def _persist_setx(name):
    return "%s %s C:\\lane\\worktree" % (_PERSIST_VERB, name)


def _persist_api(name, scope):
    return "[Environment]::%s('%s', 'C:\\lane\\worktree', '%s')" % (
        _PERSIST_API,
        name,
        scope,
    )


# S106: the ONE read-only auth verb the register leaves open, assembled from fragments so
# the literal never appears whole -- for the same reason as the two above.
READONLY_AUTH_STATUS = " ".join(("claude", "au" + "th", "status"))


# ------------------------------------------------- NA-10 (O129, hub extension): the gate
#
# The targets sit under {REPO} -- the hook's OWN worktree root, derived from the script's
# location -- because that is literally what "a lane editing its own gate" is, and because
# NA-7 allows a lane's own worktree by construction, so the DENY is attributable to NA-10
# and to nothing else.  The DENY rows' venue is that same worktree; the ALLOW rows' venue is
# the board root, which is where hook evolution is authored.
NA10_SETTINGS = "{REPO}/.claude/settings.json"
NA10_HOOK_SCRIPT = "{REPO}/tools/hooks/mlv-never-authorized.py"
NA10_VERIFIER = "{REPO}/tools/gates/verify_consented_footage.py"
NA10_UNRELATED = "{REPO}/src/mlv/video_mlv.c"
NA10_NEW_TEXT = "# rewritten by the lane this gate governs\n"


# --------------------------------------------------------------------------- table
#
# Columns: name | tool | input (or raw stdin) | EXPECTED | na (register row, DENY only)
# | fixture.  The EXPECTED column is explicit on every row, and the coverage test at the
# bottom fails the suite if any of NA-1,2,3,4,6,7,8,9 has zero DENY cases.

CASES = [
    # ------------------------------------------------------- the 3 DENY controls
    {
        "name": "control claude auth logout",
        "group": "control",
        "tool": "Bash",
        "input": {"command": "claude auth logout"},
        "expect": "DENY",
        "na": "NA-3",
        # Attributable to the register's login|logout arm, not to the fail-closed arm that
        # catches an unrecognised auth verb -- see the `na3` group below.
        "reason_contains": "login|logout",
    },
    {
        "name": "control anthropic api key assignment",
        "group": "control",
        "tool": "Bash",
        "input": {"command": "ANTHROPIC_API_KEY=sk-probe claude -p hello"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "control factory write via Write tool",
        "group": "control",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/.factory/state.yaml", "content": "revision: 9\n"},
        "expect": "DENY",
        "na": "NA-7",
    },
    # -------------------------------------------- the 16 round-1 falsifiers (ALLOWed
    # by the global hook at ratify-e05b81cc-20260906T0440Z; this hook must DENY each)
    {
        "name": "r1 git push force fork master",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "git push --force fork master"},
        "expect": "DENY",
        "na": "NA-1",
    },
    {
        "name": "r1 git reset hard then push -f",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "git reset --hard HEAD~3 && git push -f fork master"},
        "expect": "DENY",
        "na": "NA-1",
    },
    {
        "name": "r1 rm -rf closeout repo-closed",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "rm -rf {BOARD}/.claude-state/closeout/repo-closed/"},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r1 rm -f the gate ledger",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": 'rm -f "{DUAL}/claude.md"'},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r1 openai key inline before codex exec",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "OPENAI_API_KEY=sk-probe codex exec 'do a thing'"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r1 export openai key",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "export OPENAI_API_KEY"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r1 anthropic base url assignment",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "ANTHROPIC_BASE_URL=https://example.invalid"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r1 codex login",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "codex login"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r1 claude code api key assignment",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "CLAUDE_CODE_API_KEY=probe-value"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r1 rm a tracked test source",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "rm tests/console/test_receipt.cpp"},
        "expect": "DENY",
        "na": "NA-6",
    },
    {
        "name": "r1 edit removing an assertion",
        "group": "round1",
        "tool": "Edit",
        "input": {
            "file_path": "{BOARD}/tests/console/tst_receipt.cpp",
            "old_string": TEST_BEFORE,
            "new_string": TEST_WEAKENED,
        },
        "expect": "DENY",
        "na": "NA-6",
    },
    {
        "name": "r1 shell redirect into factory state",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "cat > {BOARD}/.factory/state.yaml"},
        "expect": "DENY",
        "na": "NA-7",
    },
    {
        "name": "r1 sed -i on a factory ledger",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": "sed -i 's/open/closed/' {BOARD}/.factory/coordination/HUB.md"},
        "expect": "DENY",
        "na": "NA-7",
    },
    {
        "name": "r1 write into another project tree",
        "group": "round1",
        "tool": "Write",
        "input": {"file_path": "{OUTSIDE}/other-project/src/main.c", "content": "int main(){}\n"},
        "expect": "DENY",
        "na": "NA-7",
    },
    {
        "name": "r1 open an unnamed clip",
        "group": "round1",
        "tool": "Bash",
        "input": {"command": 'mlv_dump -v "{CACHE}/2026-09-06/take07.mlv"'},
        "expect": "DENY",
        "na": "NA-4",
    },
    {
        "name": "r1 set-itemproperty usergpupreferences",
        "group": "round1",
        "tool": "PowerShell",
        "input": {
            "command": "Set-ItemProperty -Path "
            "'HKCU:\\Software\\Microsoft\\DirectX\\UserGpuPreferences' "
            "-Name MLVApp.exe -Value 'GpuPreference=2;'"
        },
        "expect": "DENY",
        "na": "NA-8",
    },
    # -------------------------------------------- the 12 round-2 falsifiers (ALLOWed
    # by the global hook at ratify-4361880b-20260906T0509Z; this hook must DENY each)
    {
        "name": "r2 git filter-branch",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "git filter-branch --tree-filter 'rm -rf big' -- --all"},
        "expect": "DENY",
        "na": "NA-1",
    },
    {
        "name": "r2 remove-item recurse fleet-runs",
        "group": "round2",
        "tool": "PowerShell",
        "input": {"command": 'Remove-Item -Recurse -Force "{BOARD}/.claude-state/fleet-runs/ws-old"'},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r2 rm -rf closeout evidence",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": 'rm -rf "{BOARD}/.closeout-evidence"'},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r2 truncate the analysis log",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "printf '' > \"{BOARD}/.claude/ANALYSIS_LOG.md\""},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r2 continue-on-error via Edit",
        "group": "round2",
        "tool": "Edit",
        "input": {
            "file_path": "{BOARD}/.github/workflows/tests.yml",
            "old_string": "      - name: Lint\n        run: python -m flake8 tools\n",
            "new_string": "      - name: Lint\n        continue-on-error: true\n"
            "        run: python -m flake8 tools\n",
        },
        "expect": "DENY",
        "na": "NA-6",
    },
    {
        "name": "r2 setx an openai variable",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "setx OPENAI_API_KEY probe-value"},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "r2 shell write outside the repo",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": 'echo hello > "{OUTSIDE}/stray.txt"'},
        "expect": "DENY",
        "na": "NA-7",
    },
    {
        "name": "r2 Write over a coordination ledger",
        "group": "round2",
        "tool": "Write",
        "input": {"file_path": "{DUAL}/claude.md", "content": "# replaced\n"},
        "expect": "DENY",
        "na": "NA-2",
        # HOOK-FALSE-POSITIVE-1: the row says `over`, so the ledger has to BE there.  Until
        # this delta the generic arm refused the path without ever opening it, and the row
        # passed against a board where `claude.md` did not exist -- a DENY row that was
        # really measuring "the hook never looks".  An ABSENT target is now a create.
        "fixture": "existing_ledger",
    },
    {
        "name": "r2 rm -rf a whole test directory",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "rm -rf tests/console"},
        "expect": "DENY",
        "na": "NA-6",
    },
    {
        "name": "r2 move-item a ledger away",
        "group": "round2",
        "tool": "PowerShell",
        "input": {"command": 'Move-Item "{DUAL}/claude.md" "{OUTSIDE}/claude.md"'},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r2 interpreter one-liner removing a ledger",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "python -c \"import os; os.remove(r'{DUAL}/claude.md')\""},
        "expect": "DENY",
        "na": "NA-2",
    },
    {
        "name": "r2 gh api delete branch protection",
        "group": "round2",
        "tool": "Bash",
        "input": {"command": "gh api -X DELETE repos/layibabalola/MLV-App/branches/master/protection"},
        "expect": "DENY",
        "na": "NA-9",
        "fixture": "snapshot",
    },
    # ------------------------------------------------------ 4 fail-closed inputs
    {
        "name": "failclosed malformed json",
        "group": "failclosed",
        "raw": "{this is not json",
        "expect": "DENY",
        "na": "hook-error",
    },
    {
        "name": "failclosed empty stdin",
        "group": "failclosed",
        "raw": "",
        "expect": "DENY",
        "na": "hook-error",
    },
    {
        "name": "failclosed missing tool_name",
        "group": "failclosed",
        "raw": json.dumps({"tool_input": {"command": "ls"}}),
        "expect": "DENY",
        "na": "hook-error",
    },
    {
        "name": "failclosed payload is not an object",
        "group": "failclosed",
        "raw": json.dumps([1, 2, 3]),
        "expect": "DENY",
        "na": "hook-error",
    },
    # ----------------------------------------------------------- NA-4 basename case
    {
        "name": "na4 same basename different clip",
        "group": "na4",
        "tool": "Bash",
        "input": {"command": 'mlv_dump -v "{BOARD}/clips/elsewhere/take01.mlv"'},
        "expect": "DENY",
        "na": "NA-4",
        "fixture": "clip_authorized",
    },
    {
        "name": "na4 the one authorized clip",
        "group": "na4",
        "tool": "Bash",
        "input": {"command": 'mlv_dump -v "{CLIP_AUTH}"'},
        "expect": "ALLOW",
        "fixture": "clip_authorized",
    },
    # ------------------------------------------- NA-2 carve-out: create vs shrink
    #
    # O125 -- THE CARVE-OUT IS A PATH PERMISSION, NOT A TOOL PERMISSION.  The first two
    # rows are the pair that proves it: the same CREATE, on a path under `$D/receipts/**`
    # that nothing exists at, differing in exactly ONE variable -- the tool.  The hook sees
    # a shell command's TEXT and never its outcome, so a truncating `Set-Content` cannot be
    # proven non-shrinking even for a create; a `Write` carries its new content in the
    # payload and can be.  This is why every Phase-0 receipt write after 0.1 uses the file
    # tools or an append, and why 0.2's canonical compound is the ONE shell write these
    # paths admit -- and only because the dedicated act is evaluated first.
    {
        "name": "carveout receipt create",
        "group": "carveout",
        "tool": "Write",
        "input": {
            "file_path": "{RECEIPTS}/0.05-hook-enforced.json",
            "content": RECEIPT_LONG,
        },
        "expect": "ALLOW",
    },
    {
        "name": "carveout receipt create via a shell Set-Content",
        "group": "carveout",
        "tool": "PowerShell",
        "input": {
            "command": "Set-Content -LiteralPath '{RECEIPTS}/x.json' -Value '"
            + RECEIPT_SHORT
            + "'"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "cannot be proven non-shrinking",
    },
    {
        "name": "carveout receipt shrink as a Write",
        "group": "carveout",
        "tool": "Write",
        "input": {"file_path": "{RECEIPTS}/live.json", "content": RECEIPT_SHORT},
        "expect": "DENY",
        "na": "NA-2",
        "fixture": "existing_receipt",
    },
    {
        "name": "carveout receipt shrink as an Edit",
        "group": "carveout",
        "tool": "Edit",
        "input": {
            "file_path": "{RECEIPTS}/live.json",
            "old_string": RECEIPT_LONG,
            "new_string": RECEIPT_SHORT,
        },
        "expect": "DENY",
        "na": "NA-2",
        "fixture": "existing_receipt",
    },
    # ------------------------------------------------- NA-2 exception (i) and (iii)
    #
    # THE 0.2 ENABLE IS ONE DEDICATED ACT (S99/O118).  There is no longer an ALLOW row for
    # a bare marker delete: the register does not authorize a verb plus a path, it
    # authorizes ONE canonical compound, and the delete lives only inside it.  Rows 1-6
    # vary the canonical shape's PRECONDITIONS; rows 7-12 vary its SHAPE, and every one of
    # those is refused BEFORE generic attribution, so the answer never depends on which
    # protected token the scan reaches first.
    {
        # The ALLOW half.  Its fixture writes the six receipts and NOT
        # `0.2-loop-enabled.json`: the enable is unspent, so exception (i) is open.
        # S101/O128: this is the FAIL-CLOSED compound -- it OPENS with
        # `$ErrorActionPreference = 'Stop'`, then `-ErrorAction Stop` on both acts with the
        # read-back `throw` between them.  Every DENY row below it varies exactly one token
        # of this shape.  S105/O126: it also runs AT THE BOARD VENUE, and the two rows
        # immediately below differ from it in that variable alone.  S112: its JSON literal is
        # now BOUND to the fixture -- `{NEWEST_CONTROL}` and `{NEWEST_CONTROL_SHA}` are
        # resolved from the receipts the fixture just wrote, so this row is ALLOW only while
        # the hook really selects the newest execution-control receipt and really hashes it.
        "name": "enable canonical fail closed compound with all six receipts",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "ALLOW",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # S105.  ONE variable from the ALLOW row: the venue is a lane's worktree.  Without
        # this test the enable is not venue-bound at all -- a worktree lane's hook evaluates
        # the SAME absolute board paths and would admit the compound the moment the six
        # receipts exist, before the hub has verified $R or installed the task.
        "name": "enable canonical compound at a worktree venue",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S105/O126)",
        "fixture": "receipts_all_six_at_worktree",
    },
    {
        # S105/O126, the ABSENT reading.  A hook invoked with no `--project-dir` has no
        # evidence of venue, and exception (i) is an exception: it must be SHOWN.  This row
        # is also what a registration that lost the argument would look like.
        "name": "enable canonical compound with the venue argument absent",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "--project-dir is <absent>",
        "fixture": "receipts_all_six",
    },
    {
        "name": "enable canonical compound with one receipt absent",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "receipt 0.6-ratio-guard.json is absent",
        "fixture": "receipts_missing_one",
    },
    {
        # S98, and it fires FIRST: the SAME six receipts, all valid, differing from the
        # ALLOW row in exactly ONE variable -- `0.2-loop-enabled.json` is now PRESENT, so
        # the single ratified authorization is spent and a RE-ARMED marker cannot be
        # deleted again.  `reason_contains` is what makes the row attributable: without it
        # it would also pass if some other arm refused.
        "name": "enable canonical compound with the enable receipt already present",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "ONE-SHOT",
        "fixture": "receipts_all_six_plus_enable",
    },
    {
        # O105: an execution-control receipt LACKING `recordedUtc` makes the newest
        # undecidable, so the whole exception fails closed -- now proved through the
        # dedicated act, which is the only surface that consults the receipt set at all.
        "name": "enable canonical compound with an execution-control receipt lacking recordedUtc",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "lacks recordedUtc",
        "fixture": "receipts_no_recorded_utc",
    },
    {
        # The two literal rows run AT THE BOARD VENUE: the venue is checked before the
        # literal, so a lane is never told that its literal was well-formed.
        "name": "enable canonical compound whose literal lacks recordedUtc",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable(literal=_enable_literal(recordedUtc=None))},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "the enable literal lacks a non-empty string recordedUtc",
        "fixture": "receipts_all_six_at_board",
    },
    {
        "name": "enable canonical compound whose literal state is not enabling",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable(literal=_enable_literal(state="enabled"))},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": 'state is not "enabling"',
        "fixture": "receipts_all_six_at_board",
    },
    # ------------------------------------------- S112: the literal is READ, not COUNTED
    #
    # Eight rows, each differing from the ALLOW row in EXACTLY ONE field of the JSON literal
    # and in nothing else -- same shape, same six receipts, same board venue.  Until this
    # delta all eight were ALLOW: the hook checked that five keys held non-empty strings and
    # nothing more, so the receipt that spends the one-shot authorization could name a file
    # that does not exist and carry a digest of nothing.  The first two rows are the OLD
    # DEFAULTS of this very table -- a receipt PATH and `"3" * 64` -- which is the sharpest
    # statement of what presence-only validation was worth.
    {
        # The SELECTION arm.  `{OLDER_CONTROL}` is a real, valid, older execution-control
        # receipt sitting beside the newest one in the same fixture: the literal must name
        # the one the hook SELECTED, not merely one that exists.
        "name": "enable canonical compound naming an older execution control receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(executionControlReceipt="{OLDER_CONTROL}")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "names executionControlReceipt '{OLDER_CONTROL}'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # A BASENAME, not a path -- and this was the table's own default until this delta,
        # which is why it is a row and not a footnote.  `receipts/<name>` names the right
        # file and is still refused: the register pins the basename, and a hook that
        # accepted both would have to decide which separator and which prefix count.
        "name": "enable canonical compound naming the execution control receipt as a path",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="receipts/{NEWEST_CONTROL}"
                )
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "names executionControlReceipt 'receipts/{NEWEST_CONTROL}'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The digest arm.  A REAL sha256 of a REAL receipt in the same directory -- just not
        # of the file the literal names.  Nothing but hashing the named file can tell these
        # two rows apart, which is the point.
        "name": "enable canonical compound whose sha256 is another receipts digest",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(executionControlSha256="{OLDER_CONTROL_SHA}")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "executionControlSha256 is '{OLDER_CONTROL_SHA}'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The SAME digest, uppercased.  The register says lowercase; the comparison is exact.
        # A value that has to be case-folded before it matches was not the pinned literal,
        # and a hook that folded it would be normalising the receipt of record.
        "name": "enable canonical compound whose sha256 is uppercase",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(executionControlSha256="{NEWEST_CONTROL_SHA_UPPER}")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "executionControlSha256 is '{NEWEST_CONTROL_SHA_UPPER}'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The correct digest, TRUNCATED to 32 characters: a prefix match would pass this and
        # a prefix match is not equality.  The row exists because "starts with" is the
        # cheapest way to get this arm subtly wrong.
        "name": "enable canonical compound whose sha256 is truncated",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(executionControlSha256="{NEWEST_CONTROL_SHA_SHORT}")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "executionControlSha256 is '{NEWEST_CONTROL_SHA_SHORT}'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The stamp arm, notation half: no trailing `Z`.  A bare local-looking stamp is not
        # a UTC stamp, and the hook does not guess which zone it was written in.
        "name": "enable canonical compound whose enabledUtc has no trailing Z",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(enabledUtc="2026-09-06T12:00:00")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "enabledUtc is '2026-09-06T12:00:00'",
            "which is not the ONE pinned notation",
            "(S112/O141)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # `+00:00` is the same INSTANT and a different NOTATION.  Accepting it would commit
        # the hook to accepting `-05:00` too, and then to converting it -- so the register's
        # one notation is the one that passes (fail-closed reading, recorded).
        "name": "enable canonical compound whose recordedUtc carries an offset",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(recordedUtc="2026-09-06T12:00:00+00:00")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "recordedUtc is '2026-09-06T12:00:00+00:00'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The stamp arm, parse half: a non-empty string that is not a timestamp at all --
        # exactly what presence-only validation accepted.
        "name": "enable canonical compound whose recordedUtc is unparseable",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(recordedUtc="not-a-timestamp")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "recordedUtc is 'not-a-timestamp'",
        "fixture": "receipts_all_six_at_board",
    },
    # ------------------------- O141: ONE notation, and the newest is the PARSED value
    #
    # Nine rows.  FIVE vary a RECEIPT stamp against the same canonical compound, the same
    # six receipts and the same board venue, so each is one variable from the ALLOW row and
    # each denies at the RECEIPT arm -- before the literal is read at all.  TWO narrow the
    # LITERAL to the same notation the receipts carry.  TWO are the ALLOW/DENY pair that
    # pins WHICH receipt the selection returns when both stamps conform.
    {
        # THE HUB'S OWN REPRODUCTION, turned into a falsifier.  `0.6` at
        # `2026-09-06T16:00:00Z` beside `0.7` at `2026-09-06T16:00:00.500000Z`: the sixth
        # commit compared these as strings, `Z` sorts after `.`, and the STALE 0.6 was
        # returned as NEWEST -- so the enable could be taken against a superseded receipt.
        # The fractional stamp is now non-conforming, the newest is UNDECIDABLE, and the
        # refusal NAMES the offending file rather than skipping it.
        "name": "enable canonical compound with a fractional receipt stamp beside a whole one",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries recordedUtc "
            "'2026-09-06T16:00:00.500000Z'",
            "UNDECIDABLE",
            "(O141)",
        ),
        "fixture": "control_stamp_fraction_at_board",
    },
    {
        # `+00:00` is the same INSTANT and a different NOTATION -- refused on a RECEIPT for
        # the same reason it is refused in the literal: accepting it commits the hook to
        # deciding what `-05:00` meant, and then to converting it.
        "name": "enable canonical compound with a receipt stamp carrying an offset",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries recordedUtc "
            "'2026-09-06T16:00:00+00:00'",
            "(O141)",
        ),
        "fixture": "control_stamp_offset_at_board",
    },
    {
        # A NAIVE stamp: no zone at all.  It is not a UTC stamp, and the hook does not guess
        # which zone a receipt was written in.
        "name": "enable canonical compound with a naive receipt stamp",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries recordedUtc "
            "'2026-09-06T16:00:00'",
            "(O141)",
        ),
        "fixture": "control_stamp_naive_at_board",
    },
    {
        # A LOWERCASE `z`.  The register pins literals -- the digest is lowercase, the zone
        # designator is uppercase -- and a value that has to be case-folded before it
        # matches is not the pinned literal.  It is also the near miss a hand-written
        # receipt is most likely to carry.
        "name": "enable canonical compound with a lowercase z receipt stamp",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries recordedUtc "
            "'2026-09-06T16:00:00z'",
            "(O141)",
        ),
        "fixture": "control_stamp_lowercase_z_at_board",
    },
    {
        # TWO receipts, ONE instant.  Both stamps conform, so the notation arm passes and
        # this row is attributable to the TIE arm alone: there is no newest, and the refusal
        # names BOTH files rather than picking whichever the directory listing reached last.
        "name": "enable canonical compound with two identically stamped receipts",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipts execution-control-0.6.json and "
            "execution-control-0.7.json carry the SAME recordedUtc '2026-09-06T16:00:00Z'",
            "UNDECIDABLE",
            "(O141)",
        ),
        "fixture": "control_stamps_tied_at_board",
    },
    {
        # The literal, narrowed.  The sixth commit accepted an optional fraction HERE while
        # never checking a receipt's stamp at all; one notation everywhere is what makes the
        # literal and the receipts comparable, so a fractional literal stamp is now DENY.
        "name": "enable canonical compound whose literal enabledUtc carries fractional seconds",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(enabledUtc="2026-09-06T12:00:00.500000Z")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "enabledUtc is '2026-09-06T12:00:00.500000Z'",
            "(S112/O141)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The same narrowing on the OTHER stamp, one row each, so a hook that narrowed only
        # the first key it read goes red here.
        "name": "enable canonical compound whose literal recordedUtc carries fractional seconds",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(recordedUtc="2026-09-06T12:00:00.500000Z")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "recordedUtc is '2026-09-06T12:00:00.500000Z'",
            "(S112/O141)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # THE ORDERING, in the ALLOW direction.  Two CONFORMING stamps one second apart --
        # `0.6` at `...16:00:00Z`, `0.7` at `...16:00:01Z` -- and the literal names
        # `{NEWEST_CONTROL}`, bound at run time by `_pinned_moment`'s own parse.  ALLOW only
        # while the hook returns `0.7`.  Its DENY partner below names `{OLDER_CONTROL}` and
        # is the half that proves the selection has a DIRECTION: reverse the comparison and
        # this row denies while that one allows.
        "name": "enable canonical compound naming the newest of two whole second stamps",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "ALLOW",
        "fixture": "control_stamps_ordered_at_board",
    },
    {
        "name": "enable canonical compound naming the older of two whole second stamps",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(executionControlReceipt="{OLDER_CONTROL}")
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "names executionControlReceipt '{OLDER_CONTROL}'",
            "the newest by PARSED recordedUtc -- is '{NEWEST_CONTROL}'",
            "(S112/O141)",
        ),
        "fixture": "control_stamps_ordered_at_board",
    },
    # ------------------- S118: the six receipts VALIDATE against the schema table
    #
    # Twenty-four DENY rows and two ALLOW rows.  Until this delta "validate" meant `json.load`
    # returned something other than `None`, and the hub reproduced what that was worth ON the
    # sixth commit: five `{}` receipts beside `execution-control-forged.json` carrying only
    # the three provenance keys ALLOWED the canonical enable, exit 0.  Every row below is one
    # key of one receipt away from the ALLOW rows, at the same board venue, with the same
    # compound and the same literal -- so each DENY is attributable to the schema arm it names.
    #
    # THE FIVE FIXED RECEIPTS, EMPTY.  One row each, because "an empty object is INVALID" has
    # to be true of every receipt and not just of whichever one the hook happens to read first.
    {
        "name": "enable canonical compound with an empty 0.18 roadmap parity receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.18-roadmap-parity.json is an EMPTY object",
            "(S118)",
        ),
        "fixture": "receipt_018_empty",
    },
    {
        "name": "enable canonical compound with an empty 0.4b required checks receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.4b-required-checks.json is an EMPTY object",
            "(S118)",
        ),
        "fixture": "receipt_04b_empty",
    },
    {
        "name": "enable canonical compound with an empty 0.4c demoted receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("receipt 0.4c-demoted.json is an EMPTY object", "(S118)"),
        "fixture": "receipt_04c_empty",
    },
    {
        "name": "enable canonical compound with an empty 0.6 ratio guard receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("receipt 0.6-ratio-guard.json is an EMPTY object", "(S118)"),
        "fixture": "receipt_06_empty",
    },
    {
        "name": "enable canonical compound with an empty 0.5 factory frozen receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.5-factory-frozen.json is an EMPTY object",
            "(S118)",
        ),
        "fixture": "receipt_05_empty",
    },
    # A REQUIRED KEY MISSING, one row per receipt, each naming the key it dropped.  A
    # non-empty object with four of five keys is what a half-written receipt looks like, and
    # it is the shape an empty-object test alone would not catch.
    {
        "name": "enable canonical compound with 0.18 lacking queueArmResultSha256",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.18-roadmap-parity.json lacks the required key queueArmResultSha256",
            "(S118)",
        ),
        "fixture": "receipt_018_missing_key",
    },
    {
        "name": "enable canonical compound with 0.4b lacking headSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.4b-required-checks.json lacks the required key headSha",
            "(S118)",
        ),
        "fixture": "receipt_04b_missing_key",
    },
    {
        "name": "enable canonical compound with 0.4c lacking mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.4c-demoted.json lacks the required key mergeSha",
            "(S118)",
        ),
        "fixture": "receipt_04c_missing_key",
    },
    {
        "name": "enable canonical compound with 0.6 lacking firstReading",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.6-ratio-guard.json lacks the required key firstReading",
            "(S118)",
        ),
        "fixture": "receipt_06_missing_key",
    },
    {
        "name": "enable canonical compound with 0.5 lacking frozenCount",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.5-factory-frozen.json lacks the required key frozenCount",
            "(S118)",
        ),
        "fixture": "receipt_05_missing_key",
    },
    # THE VALUE CLASSES.  Each row carries a value of the right SORT and the wrong CLASS --
    # the near misses a hand-written receipt actually carries.
    {
        # The right digest of the right file, uppercased.  `Get-FileHash` returns uppercase
        # (O145), so this is the value a receipt written without `.ToLowerInvariant()` holds.
        "name": "enable canonical compound with an uppercase sha256 in a receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.18-roadmap-parity.json carries queueArmResultSha256",
            "not a 64-character LOWERCASE hex sha256",
            "(S118)",
        ),
        "fixture": "receipt_sha256_uppercase",
    },
    {
        # 63 of the right 64 characters: a `startswith` test takes it, and a prefix is not
        # equality -- the same trap the S112 truncated-digest row closes on the literal.
        "name": "enable canonical compound with a 63 character sha256 in a receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.5-factory-frozen.json carries dryRunDiffSha256",
            "not a 64-character LOWERCASE hex sha256",
            "(S118)",
        ),
        "fixture": "receipt_sha256_63_chars",
    },
    {
        # `path` is an EXISTENCE test, not a shape test.  A receipt naming evidence that was
        # never written is a receipt of nothing -- and it is what an interrupted step leaves.
        "name": "enable canonical compound with a receipt path that does not exist",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.18-roadmap-parity.json carries composedPromptPath",
            "a path to a file that EXISTS",
            "(S118)",
        ),
        "fixture": "receipt_path_missing",
    },
    {
        # The UPSTREAM host.  The board carries two remotes and no default repo, so an
        # unpinned `gh` resolves to `ilia3101/MLV-App` (O135): a run URL on that host is
        # what a receipt written by an unpinned command actually contains.
        "name": "enable canonical compound with a receipt url on the wrong host",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.4c-demoted.json carries runUrl",
            "a url beginning https://github.com/layibabalola/MLV-App/",
            "(S118)",
        ),
        "fixture": "receipt_url_wrong_host",
    },
    {
        "name": "enable canonical compound with frozenCount as a string",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.5-factory-frozen.json carries frozenCount '12'",
            "a NON-NEGATIVE integer",
            "(S118)",
        ),
        "fixture": "receipt_frozen_count_string",
    },
    {
        # `True == 1` in Python, so `isinstance(value, int)` accepts a boolean -- the one
        # value class where the obvious implementation admits the wrong type outright.
        "name": "enable canonical compound with frozenCount as a boolean",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.5-factory-frozen.json carries frozenCount True",
            "a boolean is not one",
            "(S118)",
        ),
        "fixture": "receipt_frozen_count_bool",
    },
    {
        # FOUR contexts where the plan pins five, `Batch Compile` KEPT -- so the row is
        # attributable to the COUNT and not to the promoted context going missing.  Four is
        # exactly what a silent removal against the live snapshot leaves behind (NA-9/O92).
        "name": "enable canonical compound with four postContexts in the 0.4b receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "receipt 0.4b-required-checks.json carries postContexts",
            "exactly 5 non-empty strings containing 'Batch Compile'",
            "(S118)",
        ),
        "fixture": "receipt_post_contexts_four",
    },
    # THE CHAIN: SIX NAMES, AND A PRESENT-BUT-INVALID RECEIPT IS UNDECIDABLE (S118/O152).
    {
        # THE HUB'S REPRODUCTION, turned into a falsifier.  `execution-control-forged.json`
        # is a name no plan step writes, and it carries a conforming stamp LATER than 0.7,
        # a well-formed `hashes` and the real provenance -- so under the old
        # `execution-control-*.json` GLOB it would have been SELECTED as the newest and the
        # one-shot enable taken against it.  The only thing wrong with it is its NAME, which
        # is why the candidate set had to become an enumeration rather than a filter.
        "name": "enable canonical compound with a forged execution control receipt name",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-forged.json is not one of the SIX "
            "chain names",
            "(S118)",
        ),
        "fixture": "control_forged_name",
    },
    {
        # `hashes` is the chain receipt's whole point: it is what the step actually
        # measured.  A receipt carrying provenance and no hashes records that a step ran and
        # not what it saw.
        "name": "enable canonical compound with a chain receipt lacking hashes",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks hashes",
            "(S118/O152)",
        ),
        "fixture": "control_without_hashes",
    },
    {
        # The provenance is BOUND, not merely well-formed.  `"f" * 64` is a perfectly shaped
        # sha256 that is the digest of nothing on this board -- which is exactly what the
        # forged set carried, and exactly what a shape-only check accepts.
        "name": "enable canonical compound with a chain receipt whose parity sha256 is unbound",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.35.json carries "
            "roadmapParityReceiptSha256",
            "is not the sha256 of 0.18-roadmap-parity.json as it is on disk",
            "(S118/O152)",
        ),
        "fixture": "control_parity_mismatch",
    },
    {
        "name": "enable canonical compound with a chain receipt whose productLiveCount is 14",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries productLiveCount "
            "14, not exactly 15",
            "(S118/O152)",
        ),
        "fixture": "control_product_live_14",
    },
    {
        # A CHAIN VIOLATION, and the row that shows the selection is a NAME and not "whichever
        # is newest".  Both receipts are valid and both stamps conform, but 0.6 is stamped
        # LATER than a valid 0.7 -- an order the chain cannot produce.  The hook does not
        # resolve it by preferring one; it declares the newest undecidable.
        "name": "enable canonical compound with a valid 0.6 stamped later than a valid 0.7",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "the parsed-newest valid execution-control receipt is execution-control-0.6.json, "
            "but the SELECTED one is execution-control-0.7.json",
            "CHAIN VIOLATION",
            "(S118)",
        ),
        "fixture": "control_06_newer_than_07",
    },
    {
        # O177: an O152 REPAIR of 0.6, ORDER-PRESERVING.  0.4b-i, 0.6 and 0.7 are all
        # present and valid; the repair's stamp sits STRICTLY BETWEEN 0.4b-i's parsed stamp
        # and a valid 0.7's, which is the shape O152's non-shrinking rewrite must take
        # (hub-measured on the tenth commit: an order-preserving repair stamp ALLOWS -- never
        # the moment of the write, which would re-create the undecidable state the repair
        # exists to clear).  0.7 is still both the parsed-newest and the SELECTED receipt.
        "name": "enable canonical compound with an O152 repair of 0.6 stamped between 0.4b-i and a valid 0.7",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "control_o152_repair_06_between_04bi_and_07",
    },
    {
        # O177's DENY partner: the SAME repair, stamped at the MOMENT OF THE WRITE instead
        # -- later than 0.7's parsed stamp, which re-creates the undecidable state O152's
        # non-shrinking rewrite exists to clear (hub-measured on the tenth commit: a
        # write-time repair stamp DENIES).  The parsed-newest valid receipt is then 0.6, a
        # CHAIN VIOLATION against the SELECTED 0.7 -- the same rule the row above proves,
        # measured on the repair path rather than on an ordinary restamp.
        "name": "enable canonical compound with the same O152 repair of 0.6 stamped at the moment of the write",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "the parsed-newest valid execution-control receipt is execution-control-0.6.json, "
            "but the SELECTED one is execution-control-0.7.json",
            "CHAIN VIOLATION",
            "(S118)",
        ),
        "fixture": "control_o152_repair_06_at_write_time",
    },
    {
        # O152, AND THE ROW THE "EXCLUDE" READING FAILS.  0.7 is PRESENT and invalid (no
        # `queueSha256`); 0.6 is valid; the literal names 0.6 by name and carries 0.6's real
        # digest.  Exclude the invalid 0.7 -- the amendment's proposal -- and the selection
        # falls back to a valid 0.6, the literal matches, and the ONE-SHOT enable fires
        # against a STALE receipt: O141's hazard in a new shape.  The strict rule refuses
        # instead, naming the offending file, and recovery is a non-shrinking rewrite of it.
        "name": "enable canonical compound with a present but invalid 0.7 beside a valid 0.6",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_6}",
                    executionControlSha256="{CONTROL_0_6_SHA}",
                )
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks queueSha256",
            "(S118/O152)",
        ),
        "fixture": "control_07_invalid_beside_valid_06",
    },
    {
        # The SELECTED receipt is absent: a valid chain that stops at 0.35, so neither 0.7
        # nor 0.6 exists.  Without this arm "the newest valid chain receipt" would quietly
        # become 0.35 and the enable would be taken three steps early.
        "name": "enable canonical compound with the selected control receipt absent",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "the SELECTED execution-control receipt execution-control-0.6.json is ABSENT",
            "(S118)",
        ),
        "fixture": "control_selected_absent",
    },
    {
        # ALLOW, 0.7 ABSENT.  A fully conforming set whose chain is `{0.35, 0.6}`: the
        # selection falls back to 0.6, which is also the parsed-newest, and the literal names
        # it by name.  Every value in every receipt is computed from the fixture at run time.
        "name": "enable canonical compound with a conforming chain of 0.35 and 0.6",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_6}",
                    executionControlSha256="{CONTROL_0_6_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_035_and_06_at_board",
    },
    {
        # ALLOW, THE WHOLE CHAIN.  All six names present, valid and ascending, so 0.7 is both
        # the parsed-newest and the SELECTED one, and the literal names it.  This is the row
        # that proves the enumeration is a set of SIX and not a set of two.
        "name": "enable canonical compound with all six conforming chain receipts",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_all_six_names_at_board",
    },
    # ------------- S120: the second key's approval of THIS head, and the EXACT fixed set
    #
    # Nine DENY rows, each one keyword of one chain receipt away from the ALLOW rows, at the
    # board venue.  Up to the eighth commit a chain receipt validated with ANY non-empty
    # `hashes` and no review at all, so a receipt hashing one file and vouched for by nobody
    # was a complete receipt.  The verdict arm reads the TERMINAL JSON block of the file
    # `solVerdictPath` names -- and every fixture verdict carries an EARLIER fenced block
    # with a BLOCKER, so the ALLOW rows below prove "terminal" and not "first".
    {
        "name": "enable canonical compound with a chain receipt lacking reviewedHeadSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks reviewedHeadSha",
            "(S120/O152)",
        ),
        "fixture": "control_without_reviewed_head",
    },
    {
        # The RIGHT head, uppercased.  A git object name is lowercase and the comparison is
        # exact -- the same reading the S112 digest arm and the S118 sha class already carry.
        "name": "enable canonical compound with an uppercase reviewedHeadSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries reviewedHeadSha",
            "not a 40-character LOWERCASE hex sha",
            "(S120/O152)",
        ),
        "fixture": "control_reviewed_head_uppercase",
    },
    {
        "name": "enable canonical compound with a chain receipt lacking solVerdictPath",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks solVerdictPath",
            "(S120/O152)",
        ),
        "fixture": "control_without_verdict_path",
    },
    {
        # A REAL verdict file, at the RIGHT head, whose terminal block is CHANGES_REQUESTED.
        # Presence of a review is not approval; the block's `verdict` is what is read.
        "name": "enable canonical compound whose sol verdict is changes requested",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries solVerdictPath",
            "carries verdict 'CHANGES_REQUESTED', not 'APPROVE'",
            "(S120/O152)",
        ),
        "fixture": "control_verdict_changes_requested",
    },
    {
        # A REAL APPROVE -- 0.6's -- named by 0.7.  The second key approved SOMETHING; it did
        # not approve this head, and a receipt is the approval of the head it hashes at.
        "name": "enable canonical compound whose sol verdict approves a different head",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries solVerdictPath",
            "carries subject_sha",
            "not this receipt's reviewedHeadSha",
            "(S120/O152)",
        ),
        "fixture": "control_verdict_of_another_head",
    },
    {
        # A MISSING key, on the receipt that is NOT selected: O152's strict rule reaches every
        # present chain receipt, so a 0.6 that measured five of six BASE files refuses the
        # enable even though 0.7 would be the one taken.
        "name": "enable canonical compound with a chain receipt whose hashes lack a fixed set path",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.6.json carries hashes lacking "
            "tools/coordination/Invoke-Lane.ps1",
            "(S120/O152)",
        ),
        "fixture": "control_hashes_missing_key",
    },
    {
        # An EXTRA key at a well-formed digest.  A receipt vouching for a script no step
        # landed is how an unreviewed file would come to carry a receipt's authority.
        "name": "enable canonical compound with a chain receipt whose hashes carry an extra path",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries hashes with "
            "tools/hooks/extra-script.py, which is not in this step's fixed set",
            "(S120/O152)",
        ),
        "fixture": "control_hashes_extra_key",
    },
    {
        # The per-step key.  0.1 runs before the composer exists and records that fact; a
        # 0.1 without it is a receipt that does not say what it found.
        "name": "enable canonical compound with execution control 0.1 lacking composerStatus",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.1.json lacks composerStatus",
            "(S120/O152)",
        ),
        "fixture": "control_01_without_composer_status",
    },
    {
        # The right DIGITS and the wrong TYPE.  `"15" != 15`, and the arm compares exactly --
        # the 14 row above is the wrong value, this one is the wrong kind of value.
        "name": "enable canonical compound with a chain receipt whose productLiveCount is the string 15",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries productLiveCount "
            "'15', not exactly 15",
            "(S118/O152)",
        ),
        "fixture": "control_product_live_string",
    },
    # ------------- O158: the BOARD is re-hashed against the SELECTED receipt's FIXED set
    #
    # Two DENY rows in which every receipt is valid and the BOARD is what changed: a file of
    # the selected receipt's set edited after the receipt recorded it, and one deleted.  No
    # receipt-only validation can see either; the re-hash walks the table's set and names the
    # path.  Both run at the board venue, because the arm sits behind the venue test.
    {
        "name": "enable canonical compound with a fixed set file drifted in the board root",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "the SELECTED execution-control receipt execution-control-0.7.json records "
            "tools/coordination/Invoke-Lane.ps1",
            "DRIFTED",
            "(O158)",
        ),
        "fixture": "control_rehash_drift_at_board",
    },
    {
        "name": "enable canonical compound with a fixed set file absent from the board root",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "the SELECTED execution-control receipt execution-control-0.7.json hashes "
            "tools/coordination/freeze-factory-cards.py, but that file is ABSENT",
            "(O158)",
        ),
        "fixture": "control_rehash_file_missing_at_board",
    },
    # ------------- S120: the COMPLETE receipt of EACH of the six steps is ALLOW
    #
    # One ALLOW per step, each carrying that step's EXACT fixed set and its own keys.  With
    # the missing-key and extra-key rows above, these pin the hook's six sets to the suite's
    # `REQUIRED_HASHES` -- two independent restatements of plan 1.3 step 5 forced to agree.
    # 0.6 and 0.7 stand alone, so each is also the SELECTED receipt whose set O158 walks.
    {
        "name": "enable canonical compound with a complete execution control 0.1 receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_01_complete_at_board",
    },
    {
        "name": "enable canonical compound with a complete execution control 0.35 receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_035_complete_at_board",
    },
    {
        "name": "enable canonical compound with a complete execution control 0.4c-i receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_04c_i_complete_at_board",
    },
    {
        "name": "enable canonical compound with a complete execution control 0.4b-i receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_04b_i_complete_at_board",
    },
    {
        "name": "enable canonical compound with a complete execution control 0.6 receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_6}",
                    executionControlSha256="{CONTROL_0_6_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_06_complete_at_board",
    },
    {
        "name": "enable canonical compound with a complete execution control 0.7 receipt",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "chain_step_07_complete_at_board",
    },
    # ------------- O159: the 0.18 repair, complete and partial
    #
    # The hub procedure measured through the hook.  `0.18-roadmap-parity.json` is rewritten
    # NON-shrinking and every chain receipt from 0.35 on is re-pointed at its new digest with
    # `queueSha256` and `productLiveCount` untouched: ALLOW, because the hook binds each
    # receipt to the bytes on disk and the bytes now agree.  Leave ONE receipt pointing at the
    # old digest and the same act is DENY naming it -- present-but-invalid, undecidable under
    # O152's strict rule -- which is why the re-pointing is one hub step and not a follow-up.
    {
        "name": "enable canonical compound after the 0.18 receipt was repaired and every chain receipt re-pointed",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "ALLOW",
        "fixture": "o159_repaired_chain_at_board",
    },
    {
        "name": "enable canonical compound after the 0.18 receipt was repaired with one chain receipt left behind",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.35.json carries "
            "roadmapParityReceiptSha256",
            "is not the sha256 of 0.18-roadmap-parity.json as it is on disk",
            "(S118/O152)",
        ),
        "fixture": "o159_partial_repair_at_board",
    },
    {
        # O171.  The SAME complete cascade, but the rewrite RESTAMPED 0.6 and 0.7 at its own
        # instant -- what a hub that stamped "at the moment it writes" would do, and the
        # register's ONE exception to that rule is exactly this cascade.  Two receipts, one
        # stamp: the newest is undecidable and the refusal names both.  Its ALLOW partner is
        # the complete-repair row above, whose fixture preserves every ORIGINAL stamp --
        # asserted by `test_the_o159_cascade_fixture_preserves_every_original_stamp_...`.
        "name": "enable canonical compound after the 0.18 repair restamped two chain receipts to one instant",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(
                literal=_enable_literal(
                    executionControlReceipt="{CONTROL_0_7}",
                    executionControlSha256="{CONTROL_0_7_SHA}",
                )
            )
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipts execution-control-0.6.json and "
            "execution-control-0.7.json carry the SAME recordedUtc '2026-09-06T17:00:00Z'",
            "UNDECIDABLE",
            "(O141)",
        ),
        "fixture": "o159_cascade_restamped_to_a_tie_at_board",
    },
    # ------------- S123: the TWO shas -- the reviewed PR head and the merge commit
    #
    # `reviewedHeadSha` is the PR HEAD sol reviewed BEFORE the merge and `mergeSha` the
    # post-merge commit the hashes were taken at.  Four DENY rows vary `mergeSha` alone, at
    # the board venue, one keyword of one chain receipt from the ALLOW rows.  Two ALLOW rows
    # pin what the hook must NOT judge: the two shas DIFFERING (the normal post-merge shape,
    # the verdict bound to the REVIEWED head and not to the merge sha) and the two shas
    # being EQUAL (a rebase; the hook does not know the merge method and judges shape only).
    {
        "name": "enable canonical compound with a chain receipt lacking mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks mergeSha",
            "(S123/O152)",
        ),
        "fixture": "control_without_merge_sha",
    },
    {
        # The NON-selected receipt.  O152's strict rule reaches every present chain receipt,
        # so a 0.6 without its merge sha refuses the enable even though 0.7 is the one taken.
        "name": "enable canonical compound with the non-selected 0.6 lacking mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.6.json lacks mergeSha",
            "(S123/O152)",
        ),
        "fixture": "control_06_without_merge_sha",
    },
    {
        # The RIGHT merge sha, uppercased -- the exactness the reviewedHeadSha row carries.
        "name": "enable canonical compound with an uppercase mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries mergeSha",
            "not a 40-character LOWERCASE hex sha",
            "(S123/O152)",
        ),
        "fixture": "control_merge_sha_uppercase",
    },
    {
        # 39 characters, on the non-selected 0.6: a prefix of a sha is not a sha.
        "name": "enable canonical compound with a 39 character mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.6.json carries mergeSha",
            "not a 40-character LOWERCASE hex sha",
            "(S123/O152)",
        ),
        "fixture": "control_06_merge_sha_39_chars",
    },
    {
        # THE NORMAL POST-MERGE SHAPE, ALLOW.  0.7's `mergeSha` is set EXPLICITLY to a sha
        # that differs from its `reviewedHeadSha`, and the verdict it names is sol's APPROVE
        # of the REVIEWED head.  A hook that compared the verdict's `subject_sha` to the
        # merge sha -- the ninth commit's one-sha reading, met by a real merge -- refuses it.
        "name": "enable canonical compound whose mergeSha differs from its reviewedHeadSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "ALLOW",
        "fixture": "control_merge_sha_differs_from_reviewed_head",
    },
    {
        # A REBASE lands the reviewed commit itself as the tip, so the two shas are EQUAL.
        # The hook does not know the merge method and judges shape only: ALLOW, and this row
        # is what keeps an inequality test from ever being added without a red test.
        "name": "enable canonical compound whose mergeSha equals its reviewedHeadSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "ALLOW",
        "fixture": "control_merge_sha_equals_reviewed_head",
    },
    # ------------- O172: the fixed-set equality proof, bound to both shas and to the hashes
    #
    # Five DENY rows, one PART of 0.7's `fixedSetEqualityProof` from the ALLOW rows, at the
    # board venue.  The hub's git assertion that the step's fixed set hashed equal at both
    # shas was, up to the tenth commit, recorded nowhere the hook could see; the proof is the
    # record, and the hook binds it to the receipt beside it -- its two shas and its `hashes`
    # -- by pure recomputation.  Every ALLOW row's proof is DERIVED by the fixture from the
    # receipt's own hashes, so a hook that stopped recomputing goes red on the pasted row.
    {
        "name": "enable canonical compound with a chain receipt lacking fixedSetEqualityProof",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json lacks fixedSetEqualityProof",
            "(O172/O152)",
        ),
        "fixture": "control_without_equality_proof",
    },
    {
        # The RIGHT proof with its digest UPPERCASED -- `Get-FileHash`'s case (O145), the
        # near miss a hand-assembled proof carries.  Shape first: refused before any part is
        # compared.
        "name": "enable canonical compound with a malformed fixedSetEqualityProof",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries fixedSetEqualityProof",
            "not the literal <reviewedHeadSha>=<mergeSha>:<digest>",
            "(O172/O152)",
        ),
        "fixture": "control_equality_proof_malformed",
    },
    {
        # A well-formed proof whose FIRST sha is 0.6's reviewed head: the record of some
        # other review, beside this receipt's own.
        "name": "enable canonical compound whose proof first sha is not its reviewedHeadSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries "
            "fixedSetEqualityProof whose FIRST sha",
            "is not this receipt's reviewedHeadSha",
            "(O172/O152)",
        ),
        "fixture": "control_equality_proof_first_sha_mismatch",
    },
    {
        "name": "enable canonical compound whose proof second sha is not its mergeSha",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries "
            "fixedSetEqualityProof whose SECOND sha",
            "is not this receipt's mergeSha",
            "(O172/O152)",
        ),
        "fixture": "control_equality_proof_second_sha_mismatch",
    },
    {
        # 0.7's own two shas with the digest of 0.35's fixed set: a proof PASTED from another
        # receipt -- well-formed, correctly bound to the shas, and the digest of a set this
        # receipt does not carry.  Nothing but recomputing from THIS receipt's `hashes` can
        # tell it from the ALLOW rows, which is the point.
        "name": "enable canonical compound whose proof digest is another receipts fixed set digest",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "execution-control receipt execution-control-0.7.json carries "
            "fixedSetEqualityProof whose digest",
            "is not the sha256 of the sorted '<sha256>  <path>' lines of its OWN hashes",
            "(O172/O152)",
        ),
        "fixture": "control_equality_proof_digest_mismatch",
    },
    # ------------- S125: the PRE-FLIGHT ARTIFACT act -- the SECOND dedicated act
    #
    # Thirteen rows.  The hub measured on the tenth commit that the FIRST of them -- the exact
    # hook-stdin JSON of the canonical compound, written by `Write` to
    # `$D/receipts/0.2-enable-preflight-input.json` at the board venue -- was ALLOWED by the
    # receipts carve-out, as an ordinary create, while the register said any other input
    # naming both paths is DENIED; and that the same content to `notes.json`, under
    # `fleet-runs/` and from a worktree venue was ALLOWED too (measured again on this repo
    # before this delta: four ALLOWs).  The act is now decided BY RULE, before generic
    # content attribution: ONE ALLOW, ten DENY each one variable from it, one DENY for the
    # wrong TOOL on the artifact's path, and one ALLOW control that keeps the carve-out a
    # carve-out.  The content is a SPEC rendered after substitution (`_preflight_content`).
    {
        "name": "preflight artifact Write with the valid stdin json at the board venue onto an absent target",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": PREFLIGHT_ARTIFACT, "content": _preflight_content()},
        "expect": "ALLOW",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The same content, another path: content attribution, which the tenth commit did
        # not have for file tools at all.  `notes.json` is on no carve-out and under no
        # protected tail -- the plain "any other input naming both paths".
        "name": "preflight artifact content written to notes json at the board root",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/notes.json", "content": _preflight_content()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "names BOTH the enable receipt receipts/0.2-loop-enabled.json and the marker "
            "WORKSTREAM-LOOP-DISABLED",
            "(S125/S99/O118)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The same content under `fleet-runs/`, a CARVE-OUT path where a create is otherwise
        # allowed: the rule is about content, and it runs before the carve-outs are consulted.
        "name": "preflight artifact content written under fleet-runs",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": "{BOARD}/.claude-state/fleet-runs/x.json",
            "content": _preflight_content(),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "names BOTH the enable receipt receipts/0.2-loop-enabled.json and the marker "
            "WORKSTREAM-LOOP-DISABLED",
            "(S125/S99/O118)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # ONE variable from the ALLOW row: the venue is a lane's worktree.
        "name": "preflight artifact Write from a worktree venue",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": PREFLIGHT_ARTIFACT, "content": _preflight_content()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "PRE-FLIGHT ARTIFACT act",
            "--project-dir is",
            "(S105/O126)",
            "(S125)",
        ),
        "fixture": "receipts_all_six_at_worktree",
    },
    {
        # ONE variable from the ALLOW row: the artifact is already on disk.  The act CREATES
        # it; the content is never read, and the refusal names the target.
        "name": "preflight artifact Write onto an existing target",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": PREFLIGHT_ARTIFACT, "content": _preflight_content()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("PRE-FLIGHT ARTIFACT act", "already EXISTS", "(S125)"),
        "fixture": "preflight_artifact_present_at_board",
    },
    {
        "name": "preflight artifact Write whose content is not json",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": PREFLIGHT_ARTIFACT,
            "content": _preflight_content(shape="not-json"),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("does not parse as exactly ONE JSON object", "(S125)"),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # Two objects, the second a byte-identical copy of the first: "exactly one" is the
        # register's word, and a parser that stopped at the first closing brace would take
        # this.
        "name": "preflight artifact Write whose content is two json objects",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": PREFLIGHT_ARTIFACT, "content": _preflight_content(shape="two")},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "does not parse as exactly ONE JSON object",
            "Extra data",
            "(S125)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The compound as a Bash input is a different act (the S99/O118 row above), so a
        # pre-flight recording it as one is the pre-flight of a different act.
        "name": "preflight artifact Write whose inner tool name is Bash",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": PREFLIGHT_ARTIFACT,
            "content": _preflight_content(tool_name="Bash"),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("tool_name is 'Bash', not 'PowerShell'", "(S125)"),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The rev-19 shape inside the artifact: the SHAPE arm, quoting the O128 gap so the
        # operator is told which token closed.
        "name": "preflight artifact Write whose command lacks the leading preference",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": PREFLIGHT_ARTIFACT,
            "content": _preflight_content(command=_rev19_enable()),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "is not the canonical enable compound",
            "does not OPEN with $ErrorActionPreference = 'Stop' (O128)",
            "(S125)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # A compound that writes and reads back a DIFFERENT receipt file name: not the shape,
        # whatever else it gets right.
        "name": "preflight artifact Write whose command names a different receipt file",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": PREFLIGHT_ARTIFACT,
            "content": _preflight_content(
                command=_canonical_enable(receipt="{RECEIPTS}/0.2-loop-enabled-copy.json")
            ),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("is not the canonical enable compound", "(S125)"),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The SEMANTIC arm: the canonical shape whose literal names an OLDER valid receipt.
        # The artifact must record the compound the enable act would accept RIGHT NOW, and
        # this is the row that proves the pre-flight runs the enable's own validation rather
        # than a shape test of its own.
        "name": "preflight artifact Write whose command names an older execution control receipt",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": PREFLIGHT_ARTIFACT,
            "content": _preflight_content(
                command=_canonical_enable(
                    literal=_enable_literal(executionControlReceipt="{OLDER_CONTROL}")
                )
            ),
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "canonical shape but is not semantically valid",
            "names executionControlReceipt '{OLDER_CONTROL}'",
            "(S125)",
        ),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The act is a `Write`.  An `Edit` of the artifact's path is a near-miss of the act,
        # refused as such -- never judged as an ordinary carve-out extend.
        "name": "preflight artifact Edit instead of Write",
        "group": "killswitch",
        "tool": "Edit",
        "input": {"file_path": PREFLIGHT_ARTIFACT, "old_string": "{}", "new_string": "{}"},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": ("this input is Edit, not Write", "(S125)"),
        "fixture": "receipts_all_six_at_board",
    },
    {
        # THE CONTROL.  An ordinary receipt create beside the six, at the same venue: the
        # carve-out still admits every OTHER create under `$D/receipts/**`, so the act did
        # not become a wall.
        "name": "ordinary receipt create beside the six receipts at the board venue",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": "{RECEIPTS}/0.3-task-installed.json", "content": RECEIPT_LONG},
        "expect": "ALLOW",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # S99.  The row the old table had as an ALLOW.  Every precondition of exception (i)
        # holds -- six receipts valid, the enable unspent -- and the delete is STILL
        # refused, because the receipts are not a standing key: they are conditions ON the
        # canonical act, and this input is not it.
        "name": "killswitch bare delete with all six receipts",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": "Remove-Item -LiteralPath '{DUAL}/WORKSTREAM-LOOP-DISABLED'"},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "outside the canonical enable is never authorized (S99)",
        "fixture": "receipts_all_six",
    },
    {
        # O118: the same two paths, the same two verbs, the ORDER reversed.  Generic
        # attribution would reach the marker token first here and the receipt token first
        # in the canonical form; neither may be what decides the enable.
        "name": "killswitch delete before the receipt write",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": "Remove-Item -LiteralPath '{DUAL}/WORKSTREAM-LOOP-DISABLED'; "
            "Set-Content -LiteralPath '{RECEIPTS}/0.2-loop-enabled.json' -Value '"
            + ENABLE_LITERAL
            + "'"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S99/O118)",
        "fixture": "receipts_all_six",
    },
    {
        "name": "killswitch enable written with Out-File instead of Set-Content",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": "$r = '"
            + ENABLE_LITERAL
            + "'; Out-File -LiteralPath '{RECEIPTS}/0.2-loop-enabled.json' -InputObject $r; "
            "Remove-Item -LiteralPath '{DUAL}/WORKSTREAM-LOOP-DISABLED'"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S99/O118)",
        "fixture": "receipts_all_six",
    },
    {
        "name": "killswitch enable written with a truncating redirect",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": "$r = '"
            + ENABLE_LITERAL
            + "'; $r > '{RECEIPTS}/0.2-loop-enabled.json'; "
            "Remove-Item -LiteralPath '{DUAL}/WORKSTREAM-LOOP-DISABLED'"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S99/O118)",
        "fixture": "receipts_all_six",
    },
    {
        # Fail-closed reading, proved: `-Path` is not `-LiteralPath`.  It resolves
        # wildcards, so it is not the act the register pinned.
        "name": "killswitch enable with Path instead of LiteralPath",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": "$r = '"
            + ENABLE_LITERAL
            + "'; Set-Content -Path '{RECEIPTS}/0.2-loop-enabled.json' -Value $r; "
            "Remove-Item -LiteralPath '{DUAL}/WORKSTREAM-LOOP-DISABLED'"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S99/O118)",
        "fixture": "receipts_all_six",
    },
    {
        # Fail-closed reading, proved: the canonical form is a PowerShell input.  The
        # byte-identical text arriving as a Bash input is a different act.
        "name": "killswitch canonical enable text delivered as a Bash input",
        "group": "killswitch",
        "tool": "Bash",
        "input": {"command": _canonical_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S99/O118)",
        "fixture": "receipts_all_six",
    },
    # ------------------------------------------- S101: the shape is FAIL-CLOSED, and only
    #
    # Four rows, each differing from the ALLOW row above in EXACTLY ONE token of the pinned
    # compound.  All four are refused on the same line, which names S101 -- the point is
    # that a near-miss of the fail-closed shape is not a degraded enable, it is an ordinary
    # non-canonical input naming both paths, and the operator is told which token closed.
    {
        # The rev-18 shape: `Set-Content` without `-ErrorAction Stop` is NON-terminating,
        # so on a failed write the pipeline continues and `Remove-Item` disarms the kill
        # switch with the receipt ABSENT -- the never-authorized act with a receipt-shaped
        # alibi, and the S98 one-shot never closes because nothing was written.  It was
        # canonical one revision ago; it is not grandfathered.
        "name": "enable rev18 shape without ErrorAction Stop or a read back",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _rev18_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S101)",
        "fixture": "receipts_all_six",
    },
    {
        # Without `-ErrorAction Stop` on the DELETE half a failed `Remove-Item` is
        # swallowed: the compound reports a success it did not have, and the receipt now
        # says the loop is enabled while the marker still stops it.
        "name": "enable compound whose Remove-Item lacks ErrorAction Stop",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable(delete_tail="")},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S101)",
        "fixture": "receipts_all_six",
    },
    {
        # The read-back must read back THE FILE THIS COMPOUND JUST WROTE.  Verifying some
        # other receipt proves that other file's contents and lets the delete run anyway --
        # precisely the failure the verification exists to catch.
        "name": "enable compound whose read back names a different receipt path",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {
            "command": _canonical_enable(readback="{RECEIPTS}/0.2-loop-enabled-copy.json")
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S101)",
        "fixture": "receipts_all_six",
    },
    {
        # The register pins the halt message as a literal.  It is what a later reader greps
        # for when asked whether an enable halted at the write, so a different string is a
        # different act -- compared case-sensitively, fail-closed.
        "name": "enable compound whose throw string differs",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable(throw="write-failed")},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "(S101)",
        "fixture": "receipts_all_six",
    },
    # ------------------------------------------- O128: the shape OPENS with the preference
    #
    # Two more rows, each one token from the ALLOW row.  They exist because `-ErrorAction
    # Stop` turned out not to be the load-bearing arm for the whole fail-closed claim: the
    # hub measured the rev-19 shape exiting 0 and DELETING THE MARKER when the receipt path's
    # drive does not resolve, because the provider dynamic parameters never bind and
    # `-ErrorAction` goes unbound with them.  A preference variable cannot come unbound.  The
    # runtime half of this claim is `EnableCompoundIsFailClosedTests` below, which RUNS it.
    {
        # The rev-19 shape: canonical one revision ago, and not grandfathered -- the same
        # fate the rev-18 shape met under S101.
        "name": "enable rev19 shape without the leading ErrorActionPreference",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _rev19_enable()},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "does not OPEN with $ErrorActionPreference = 'Stop' (O128)",
        "fixture": "receipts_all_six_at_board",
    },
    {
        # The statement is present and the VALUE is the arm turned off.  `Continue` is the
        # PowerShell default, so this compound behaves exactly like the rev-19 shape while
        # looking like the current one -- which is precisely why the value is pinned.
        "name": "enable compound whose leading preference is not Stop",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": _canonical_enable(preference="Continue")},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "opens with a preference of 'Continue'",
        "fixture": "receipts_all_six_at_board",
    },
    {
        "name": "killswitch create via Write with no receipts",
        "group": "killswitch",
        "tool": "Write",
        "input": {"file_path": "{DUAL}/WORKSTREAM-LOOP-DISABLED", "content": "stopped\n"},
        "expect": "ALLOW",
    },
    {
        "name": "killswitch create via Set-Content with no receipts",
        "group": "killswitch",
        "tool": "PowerShell",
        "input": {"command": "Set-Content -Path \"{DUAL}/WORKSTREAM-LOOP-DISABLED\" -Value 'stopped'"},
        "expect": "ALLOW",
    },
    {
        # The generic carve-out, kept EXPLICIT so it is visible that writing the enable
        # receipt on its own is ordinary create-or-extend under `$D/receipts/**` -- and
        # that a two-call sequence (this, then a delete) is therefore NOT what authorizes
        # the delete: the second call is a bare delete, which the S99 row above refuses.
        "name": "enable receipt written on its own with the receipt absent",
        "group": "killswitch",
        "tool": "Write",
        "input": {
            "file_path": "{RECEIPTS}/0.2-loop-enabled.json",
            "content": ENABLE_LITERAL + "\n",
        },
        "expect": "ALLOW",
        "fixture": "receipts_all_six",
    },
    # ----------------------------------------------------------- NA-2 exception (iv)
    #
    # O124/O126 -- THE VENUE, NOT THE TOOL AND NOT THE LENGTH, AND IT RIDES ON ARGV.  Once
    # the hook is live in the hub's session every route to amending a manifest surface under
    # `$D` is denied, yet re-ratification (plan 1.3) and 0.2's interruption recovery both
    # require exactly that.  What separates the amendment actor from a lane is where the
    # SESSION is rooted: `--project-dir` is substituted by Claude Code from
    # `${CLAUDE_PROJECT_DIR}` in the REGISTERED command, no lane is ever rooted at the board
    # (the wrapper refuses `workdir-is-board-root`), and a lane's tool input cannot set a
    # command-line argument.  It is an ARGUMENT and not a variable because the variable was
    # measured absent from real hook processes -- the last row of this group is the falsifier
    # for that regression, and it FAILS if the hook ever reads the environment again.
    #
    # The sixteen ALLOW rows are the cross product of the four ratified surfaces and four
    # write shapes -- a LONGER `Write`, a SHORTER `Write`, an `Edit`, and a shell truncating
    # `Set-Content`.  The shorter rows are load-bearing: exception (iv) is "any length", so
    # a surface is NOT on the shrink guard, and the shell row is load-bearing because O125
    # otherwise denies a shell truncating write on a protected path outright.  The five
    # DENY/control rows that follow differ from the plan ALLOW rows in exactly ONE variable
    # -- the venue, where the venue came from, or the verb.
    {
        "name": "manifest plan Write longer at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PLAN, "content": MANIFEST_LONGER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest plan Write shorter at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PLAN, "content": MANIFEST_SHORTER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest plan Edit at the board venue",
        "group": "manifest",
        "tool": "Edit",
        "input": {
            "file_path": MANIFEST_PLAN,
            "old_string": MANIFEST_EXISTING,
            "new_string": MANIFEST_SHORTER,
        },
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest plan shell Set-Content at the board venue",
        "group": "manifest",
        "tool": "PowerShell",
        "input": {"command": _set_content(MANIFEST_PLAN)},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest register Write longer at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_REGISTER, "content": MANIFEST_LONGER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest register Write shorter at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_REGISTER, "content": MANIFEST_SHORTER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest register Edit at the board venue",
        "group": "manifest",
        "tool": "Edit",
        "input": {
            "file_path": MANIFEST_REGISTER,
            "old_string": MANIFEST_EXISTING,
            "new_string": MANIFEST_SHORTER,
        },
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest register shell Set-Content at the board venue",
        "group": "manifest",
        "tool": "PowerShell",
        "input": {"command": _set_content(MANIFEST_REGISTER)},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest wrapper Write longer at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_WRAPPER, "content": MANIFEST_LONGER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest wrapper Write shorter at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_WRAPPER, "content": MANIFEST_SHORTER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest wrapper Edit at the board venue",
        "group": "manifest",
        "tool": "Edit",
        "input": {
            "file_path": MANIFEST_WRAPPER,
            "old_string": MANIFEST_EXISTING,
            "new_string": MANIFEST_SHORTER,
        },
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest wrapper shell Set-Content at the board venue",
        "group": "manifest",
        "tool": "PowerShell",
        "input": {"command": _set_content(MANIFEST_WRAPPER)},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest prompt card Write longer at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PROMPT, "content": MANIFEST_LONGER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest prompt card Write shorter at the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PROMPT, "content": MANIFEST_SHORTER},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest prompt card Edit at the board venue",
        "group": "manifest",
        "tool": "Edit",
        "input": {
            "file_path": MANIFEST_PROMPT,
            "old_string": MANIFEST_EXISTING,
            "new_string": MANIFEST_SHORTER,
        },
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        "name": "manifest prompt card shell Set-Content at the board venue",
        "group": "manifest",
        "tool": "PowerShell",
        "input": {"command": _set_content(MANIFEST_PROMPT)},
        "expect": "ALLOW",
        "fixture": "manifest_at_board",
    },
    {
        # ONE variable away from the first ALLOW row: the venue is a lane's `C:\mlvtmp\`
        # worktree, which a lane's hook receives and cannot change.
        "name": "manifest plan Write at a worktree venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PLAN, "content": MANIFEST_LONGER},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "exception (iv)",
        "fixture": "manifest_at_worktree",
    },
    {
        # ABSENT is not "unknown, assume the hub".  A hook invoked without the argument has
        # no evidence of venue, and exception (iv) is an exception: it must be shown.
        "name": "manifest plan Write with the venue argument absent",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PLAN, "content": MANIFEST_LONGER},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "<absent>",
        "fixture": "manifest_venue_absent",
    },
    {
        # O126, THE REGRESSION FALSIFIER.  `CLAUDE_PROJECT_DIR` is set IN THE ENVIRONMENT to
        # the board root and NO `--project-dir` argument is passed.  The rev-19 hook read
        # exactly that variable, and it was measured absent from every real hook process, so
        # the mechanism it keyed on could never fire.  This row is DENY, and it is the row
        # that goes red the moment anyone reintroduces an environment read of the venue.
        "name": "manifest plan Write with the venue only in the environment",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": MANIFEST_PLAN, "content": MANIFEST_LONGER},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "<absent>",
        "fixture": "manifest_venue_env_only",
    },
    {
        # Delete/move stays DENIED at EVERY venue, board included: the guard on an
        # amendment is that plan 1.3 re-hashes and re-ratifies it, and a deletion leaves
        # nothing to re-hash.
        "name": "manifest register delete from the board venue",
        "group": "manifest",
        "tool": "PowerShell",
        "input": {"command": "Remove-Item -LiteralPath '" + MANIFEST_REGISTER + "'"},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "denied at EVERY venue",
        "fixture": "manifest_at_board",
    },
    {
        # THE CONTROL.  A non-surface path under the SAME `$D`, at the SAME board venue:
        # `queue.json` is on the O47 carve-out, so it keeps its shrink guard and the venue
        # changes nothing.  Without this row, exception (iv) could quietly have become
        # "the board root may do anything under $D".
        "name": "manifest control queue json shrink is unchanged by the board venue",
        "group": "manifest",
        "tool": "Write",
        "input": {"file_path": "{DUAL}/queue.json", "content": MANIFEST_SHORTER},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "shrinking overwrite",
        "fixture": "manifest_at_board",
    },
    # ------------------------------------------------------------ NA-2 exception (ii)
    {
        "name": "checkpoint stub with a sha-matching archive copy",
        "group": "checkpoint",
        "tool": "Write",
        "input": {
            "file_path": "{DUAL}/orchestrator-resume-CURRENT.md",
            "content": "# archived: {CHECKPOINT_SHA}\n",
        },
        "expect": "ALLOW",
        "fixture": "checkpoint_archived",
    },
    {
        "name": "checkpoint stub with no archive copy",
        "group": "checkpoint",
        "tool": "Write",
        "input": {
            "file_path": "{DUAL}/orchestrator-resume-CURRENT.md",
            "content": "# archived: {CHECKPOINT_SHA}\n",
        },
        "expect": "DENY",
        "na": "NA-2",
        "fixture": "checkpoint_unarchived",
    },
    # ------------------------------------------------------------------------ NA-9
    {
        "name": "na9 canonical 0.4b body with its receipt conditions satisfied",
        "group": "na9",
        "tool": "Bash",
        "input": {"command": CANONICAL_04B_PATCH},
        "expect": "ALLOW",
        "fixture": "receipts_04b_ready",
    },
    {
        "name": "na9 canonical 0.4b body with the 0.4b receipt already present",
        "group": "na9",
        "tool": "Bash",
        "input": {"command": CANONICAL_04B_PATCH},
        "expect": "DENY",
        "na": "NA-9",
        "fixture": "receipts_04b_already_done",
    },
    {
        "name": "na9 single context removal against the snapshot",
        "group": "na9",
        "tool": "Bash",
        "input": {"command": REMOVAL_PATCH},
        "expect": "DENY",
        "na": "NA-9",
        "fixture": "snapshot",
    },
    {
        "name": "na9 add-only body with the snapshot absent",
        "group": "na9",
        "tool": "Bash",
        "input": {"command": ADD_ONLY_PATCH},
        "expect": "DENY",
        "na": "NA-9",
        "fixture": "snapshot_absent",
    },
    {
        "name": "na9 add-only body with a malformed final snapshot row",
        "group": "na9",
        "tool": "Bash",
        "input": {"command": ADD_ONLY_PATCH},
        "expect": "DENY",
        "na": "NA-9",
        "fixture": "snapshot_malformed_tail",
    },
    # ------------------------------------------------------- NA-6 the MOVE exception
    {
        "name": "na6 test step moved with its run body preserved",
        "group": "na6",
        "tool": "Edit",
        "input": {
            "file_path": "{BOARD}/.github/workflows/tests.yml",
            "old_string": WORKFLOW_BEFORE,
            "new_string": WORKFLOW_MOVED,
        },
        "expect": "ALLOW",
    },
    {
        "name": "na6 test step deleted outright",
        "group": "na6",
        "tool": "Edit",
        "input": {
            "file_path": "{BOARD}/.github/workflows/tests.yml",
            "old_string": WORKFLOW_BEFORE,
            "new_string": WORKFLOW_DELETED,
        },
        "expect": "DENY",
        "na": "NA-6",
    },
    # -------------------------------- NA-3's `claude auth` arm, bounded by the REGISTER
    #
    # `never-authorized.json` v21 NA-3 names `claude auth login|logout`, not `claude auth`.
    # These four rows differ from one another in exactly ONE variable -- the auth
    # subcommand -- so they prove the arm is a GATE and not a wall.  The ALLOW row is
    # load-bearing: the board's account-rotation procedure runs `claude auth status --json`
    # from the hub session, which is under this hook from 0.1 onward, and the shipped
    # catch-all denied it.  The last row records the fail-CLOSED reading of the gap the
    # register leaves: an auth invocation with no recognised read-only verb still DENIES.
    #
    # They are a group of their own rather than additions to `benign`/`control`, because
    # `test_table_counts_match_the_ratification_receipts` pins those counts to the 0.05
    # ratification receipts.  Those counts are a historical fact about what was ratified;
    # growing them would erase it.  (The `claude auth logout` DENY control already covers
    # the logout verb and stays exactly where it is, at the top of the table.)
    {
        "name": "na3 claude auth login",
        "group": "na3",
        "tool": "Bash",
        "input": {"command": "claude auth login"},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "login|logout",
    },
    {
        "name": "na3 claude auth login with flags",
        "group": "na3",
        "tool": "PowerShell",
        "input": {"command": "claude auth login --email someone@example.invalid"},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "login|logout",
    },
    {
        "name": "na3 claude auth status json is read only",
        "group": "na3",
        "tool": "Bash",
        "input": {"command": "claude auth status --json"},
        "expect": "ALLOW",
    },
    {
        "name": "na3 claude auth with no subcommand fails closed",
        "group": "na3",
        "tool": "Bash",
        "input": {"command": "claude auth"},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "fail-closed",
    },
    {
        # S106, the register's own carve-out, in its BARE form and assembled from fragments.
        # The card lists it among the benign controls; it lives HERE instead because
        # `test_table_counts_match_the_ratification_receipts` pins `benign` to 6 as a
        # historical fact about what the 0.05 ratification measured, and growing that count
        # would erase the fact rather than record a new one.  Same rule, same evidence, a
        # group that is allowed to grow.
        "name": "na3 claude auth status bare is read only",
        "group": "na3",
        "tool": "Bash",
        "input": {"command": READONLY_AUTH_STATUS},
        "expect": "ALLOW",
    },
    # ------------------------------- NA-3 / O129: PERSISTENT scope on the hook's own inputs
    #
    # Five DENY rows, one per name, both persistent verbs covered across them, plus one
    # ALLOW control.  The control is the load-bearing row: a persistent write is denied
    # because it is INHERITED BY EVERY LATER HOOK PROCESS and moves the board root the rules
    # are judged against, while a plain in-process `$env:` assignment dies with its shell and
    # reaches no hook at all.  Without the control this rule could quietly widen into "never
    # mention these names", which would break the test harness that must set them.
    {
        "name": "o129 persistent user scope assignment of the venue name",
        "group": "na3_persistent",
        "tool": "PowerShell",
        "input": {"command": _persist_api("CLAUDE_PROJECT_DIR", "User")},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT CLAUDE_PROJECT_DIR",
    },
    {
        "name": "o129 persistent setx of the board root name",
        "group": "na3_persistent",
        "tool": "PowerShell",
        "input": {"command": _persist_setx("MLV_BOARD_ROOT")},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_BOARD_ROOT",
    },
    {
        "name": "o129 persistent setx of the lane prompt name",
        "group": "na3_persistent",
        "tool": "Bash",
        "input": {"command": _persist_setx("MLV_LANE_PROMPT")},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_LANE_PROMPT",
    },
    {
        "name": "o129 persistent machine scope assignment of the snapshot name",
        "group": "na3_persistent",
        "tool": "PowerShell",
        "input": {"command": _persist_api("MLV_REQUIRED_CHECKS_SNAPSHOT", "Machine")},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_REQUIRED_CHECKS_SNAPSHOT",
    },
    {
        # Case-insensitivity, proved rather than asserted: Windows variable names and
        # `[EnvironmentVariableTarget]` both parse case-insensitively, so a lowercase form is
        # the same act and must give the same answer.
        "name": "o129 persistent assignment of the clip cache name in lower case",
        "group": "na3_persistent",
        "tool": "PowerShell",
        "input": {"command": _persist_api("mlv_clip_cache_root", "user")},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_CLIP_CACHE_ROOT",
    },
    {
        "name": "o129 control in process env assignment of the board root name",
        "group": "na3_persistent",
        "tool": "PowerShell",
        "input": {"command": "$env:MLV_BOARD_ROOT = 'C:/lane/worktree'"},
        "expect": "ALLOW",
    },
    # --------------------------------------- S131: the `.exe` and fully-qualified bypasses
    #
    # Round-27 sol-key finding, reproduced by the hub: `setx.exe <NAME>` (the explicit form
    # of the same Windows binary `setx` resolves to) and
    # `[System.Environment]::SetEnvironmentVariable(...)` (the fully-qualified form of the
    # same .NET API as `[Environment]::SetEnvironmentVariable`) were NOT denied, while their
    # bare/unqualified forms were -- two bypasses of the same act, not two different acts.
    # A group of its own rather than an addition to `round2` or `na3_persistent`: those
    # counts are historical facts about earlier ratifications and must not move.
    {
        "name": "s131 setx.exe of the board root name is the same act as setx",
        "group": "s131",
        "tool": "PowerShell",
        "input": {"command": "%s.exe MLV_BOARD_ROOT x" % _PERSIST_VERB},
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_BOARD_ROOT",
    },
    {
        "name": "s131 setx.exe of an openai credential is the same act as setx",
        "group": "s131",
        "tool": "Bash",
        "input": {"command": "%s.exe OPENAI_API_KEY x" % _PERSIST_VERB},
        "expect": "DENY",
        "na": "NA-3",
    },
    {
        "name": "s131 fully qualified SetEnvironmentVariable is the same act as unqualified",
        "group": "s131",
        "tool": "PowerShell",
        "input": {
            "command": "[System.Environment]::%s('MLV_BOARD_ROOT', 'x', "
            "[System.EnvironmentVariableTarget]::User)" % _PERSIST_API
        },
        "expect": "DENY",
        "na": "NA-3",
        "reason_contains": "PERSISTENT MLV_BOARD_ROOT",
    },
    # ------------------------------------------------- NA-10: a lane editing its own gate
    #
    # O129, hub extension.  This hook and `.claude/settings.json` are BOTH re-read on every
    # tool call, so a lane's edit of either takes effect on its NEXT call -- and nothing else
    # in the register covered them: NA-2 guards `.claude-state/**`, NA-7 explicitly ALLOWS
    # the lane's own worktree, NA-6 is about tests.  The predicate is (TAIL, VENUE): the same
    # write is DENY at a worktree venue and ALLOW at the board venue, which is the only
    # difference between the first two rows and the two after them.  The last row is the
    # control that keeps this a rule about the GATE rather than about the worktree.
    {
        "name": "na10 lane rewrites its own settings json",
        "group": "na10",
        "tool": "Write",
        "input": {"file_path": NA10_SETTINGS, "content": NA10_NEW_TEXT},
        "expect": "DENY",
        "na": "NA-10",
        "reason_contains": "OWN gate",
        "fixture": "venue_at_worktree",
    },
    {
        "name": "na10 lane rewrites the hook script that governs it",
        "group": "na10",
        "tool": "Write",
        "input": {"file_path": NA10_HOOK_SCRIPT, "content": NA10_NEW_TEXT},
        "expect": "DENY",
        "na": "NA-10",
        "reason_contains": "OWN gate",
        "fixture": "venue_at_worktree",
    },
    {
        "name": "na10 settings json written at the board venue",
        "group": "na10",
        "tool": "Write",
        "input": {"file_path": NA10_SETTINGS, "content": NA10_NEW_TEXT},
        "expect": "ALLOW",
        "fixture": "venue_at_board",
    },
    {
        "name": "na10 hook script written at the board venue",
        "group": "na10",
        "tool": "Write",
        "input": {"file_path": NA10_HOOK_SCRIPT, "content": NA10_NEW_TEXT},
        "expect": "ALLOW",
        "fixture": "venue_at_board",
    },
    {
        # The shell arm.  A hook is neutered as effectively by `Set-Content` as by `Write`,
        # and the register's "any write" is a predicate on the PATH, not on the tool.
        "name": "na10 shell set content over the hook script",
        "group": "na10",
        "tool": "PowerShell",
        "input": {"command": _set_content(NA10_HOOK_SCRIPT)},
        "expect": "DENY",
        "na": "NA-10",
        "reason_contains": "OWN gate",
        "fixture": "venue_at_worktree",
    },
    {
        # NA4-OWNER-CONSENTED-FOOTAGE-1 round 3 (E): the consent table's content verifier is
        # the fourth gate tail.  A lane that rewrites it rewrites the content half of NA-4.
        "name": "na10 lane rewrites the consented footage verifier",
        "group": "na10",
        "tool": "Write",
        "input": {"file_path": NA10_VERIFIER, "content": NA10_NEW_TEXT},
        "expect": "DENY",
        "na": "NA-10",
        "reason_contains": "OWN gate",
        "fixture": "venue_at_worktree",
    },
    {
        # THE CONTROL.  An ordinary source edit in the same worktree at the same venue: NA-10
        # is about four files, not about a lane's right to edit code.
        "name": "na10 control unrelated worktree edit at a lane venue",
        "group": "na10",
        "tool": "Edit",
        "input": {
            "file_path": NA10_UNRELATED,
            "old_string": "int decode(void) { return 0; }\n",
            "new_string": "int decode(void) { return 1; }\n",
        },
        "expect": "ALLOW",
        "fixture": "venue_at_worktree",
    },
    # ------------------------------------ HOOK-FALSE-POSITIVE-1: the falsifier PAIRS
    #
    # MEASURED, NOT ARGUED.  On 2026-09-07/08 the hub measured this gate refusing read-only
    # and create-only acts: fourteen of twenty-seven headless Sonnet editing runs ended at
    # `max_turns` after a denial, and the orchestrator was refused five times.  Every pair
    # below is an ALLOW SUBJECT beside a DENY CONTROL, so a hook that simply stopped
    # enforcing goes RED on the control rather than looking fixed.  Every subject was
    # measured DENY on the parent commit (3e2b2220) except where a row says otherwise.
    #
    # PAIR DISCIPLINE (round 2, sol's MAJOR on PR #104).  Round 1 asserted "exactly one
    # load-bearing token" for all six pairs and delivered it for one.  Each pair below now
    # NAMES the token it varies, and the two pairs that cannot be one-token say so in their
    # own comment instead of claiming it.  The claim is falsifiable by reading the two
    # `input` dicts side by side; keep it that way.
    #
    # (i) THE REDIRECT'S TARGET IS THE ONLY THING IT TRUNCATES.  `2>` sets the truncation
    # act, and up to this delta the act was attributed to every protected token on the
    # line -- so reading a ledger with stderr discarded was "a truncating write" OF that
    # ledger.  VARIES EXACTLY ONE TOKEN: the `2>` destination.  Same command, same verb,
    # same protected path in the same read position; only the sink becomes the ledger, and
    # a shell really does truncate what `2>` names.
    {
        "name": "fp1 i read a ledger with stderr to the bit bucket",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "cat \"{DUAL}/queue.json\" 2>/dev/null"},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 i CTL the same read with the ledger as the stderr target",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "cat \"{DUAL}/queue.json\" 2>\"{DUAL}/queue.json\""},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "cannot be proven non-shrinking",
    },
    # (ii) THE SAME CLAIM FOR STDOUT, BESIDE `2>&1`.  The subject truncates something -- an
    # unprotected scratch file -- so the act really is present; what changed is WHICH token
    # it is attributed to.  VARIES EXACTLY ONE TOKEN: the `>` destination.  Round 1's
    # control also swapped the file being READ, which meant the pair could have passed for
    # a reason that had nothing to do with attribution.
    {
        "name": "fp1 ii read a ledger into a scratch file with stderr merged",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {
            "command": "cat \"{DUAL}/queue.json\" > \"{BOARD}/scratch/out.txt\" 2>&1"
        },
        "expect": "ALLOW",
    },
    {
        "name": "fp1 ii CTL the same read with the ledger as the redirect target",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {
            "command": "cat \"{DUAL}/queue.json\" > \"{DUAL}/queue.json\" 2>&1"
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "cannot be proven non-shrinking",
    },
    # (iii) AN INTERPRETER METHOD NAME IS NOT A SHELL TRUNCATION VERB.  The heredoc READS
    # the ledger and writes an UNPROTECTED continuity path; its `if len(r) > 0` is what set
    # the truncation act, and the ledger is named only as the thing being read.  The hook
    # still does not decode interpreter bodies -- that limit is unchanged and deliberate --
    # so the control is a real `Set-Content` of the ledger, which the text matcher DOES see.
    #
    # NOT A ONE-TOKEN PAIR, AND IT CANNOT BE.  The tool, the command and the payload all
    # differ, because the claim IS that an interpreter body and a shell cmdlet are different
    # CLASSES to a text matcher: no single token turns a heredoc into a `Set-Content`, and
    # any mutation of the heredoc that stayed a heredoc would still be ALLOW and so would
    # not be a control at all.  Recorded here rather than papered over -- round 1 listed
    # this pair as one-token and it never was.
    {
        "name": "fp1 iii heredoc reading a ledger and writing an unprotected path",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {
            "command": "py -3 - <<'PY'\n"
            "from pathlib import Path\n"
            "q = Path(r'{DUAL}/queue.json')\n"
            "rows = [r for r in q.read_text().splitlines() if len(r) > 0]\n"
            "Path(r'{BOARD}/.claude-state/continuity/x.json').write_bytes(b'ok')\n"
            "PY"
        },
        "expect": "ALLOW",
    },
    {
        "name": "fp1 iii CTL a real Set-Content of that ledger",
        "group": "falsepositive1",
        "tool": "PowerShell",
        "input": {"command": "Set-Content -LiteralPath '{DUAL}/queue.json' -Value 'x'"},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "cannot be proven non-shrinking",
    },
    # (iv) A CREATE OVERWRITES NOTHING.  The generic arm's refusal names OVERWRITING, and it
    # fired on a `Write` of a path nothing existed at -- the orchestrator's five refusals
    # were all this shape.  THREE one-token pairs, one per thing the allowance is bounded
    # by, because round 1's single control varied the directory, the basename AND the
    # payload at once and so proved none of them:
    #   iv-a VARIES EXACTLY ONE TOKEN, the BASENAME -- same directory, same content, and
    #        the control's name is a chain receipt, held back from the allowance by NAME.
    #   iv-b VARIES EXACTLY ONE TOKEN, the DIRECTORY segment -- same basename, same
    #        content, and the control's directory is `receipts/`, held back by PATH.
    #   iv-d VARIES EXACTLY ONE TOKEN, the PAYLOAD -- same tool, same existing pen, and
    #        only the content shrinks, which is the untouched shrink guard.
    {
        "name": "fp1 iv a Write creates a new coordination file",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{DUAL}/OPUS-HUB-PASTE.md", "content": CHECKPOINT_LONG},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 iv a CTL the same create under a chain receipt basename",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{DUAL}/execution-control-forged.json",
            "content": CHECKPOINT_LONG,
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "overwriting ledger/pen/receipt/evidence content",
    },
    {
        "name": "fp1 iv b Write creates a file under an ordinary coordination directory",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{BOARD}/.claude-state/coordination/handoff/notes.json",
            "content": CHECKPOINT_LONG,
        },
        "expect": "ALLOW",
    },
    {
        "name": "fp1 iv b CTL the same create under a receipts directory",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{BOARD}/.claude-state/coordination/receipts/notes.json",
            "content": CHECKPOINT_LONG,
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "overwriting ledger/pen/receipt/evidence content",
    },
    {
        # UNCHANGED BY THIS DELTA, PINNED SO NOBODY READS THE CONTROLS ABOVE AS BROADER
        # THAN THEY ARE.  The SAME forged name under `$D/receipts/**` reaches the RECEIPTS
        # CARVE-OUT one branch earlier and has always been ALLOWED as a create -- measured
        # ALLOW on 3e2b2220 and still ALLOW here.  A forged chain name is defended at the
        # ENABLE ACT (S118: it is not one of the six, so it fails the exception closed by
        # name), never at write time, because the hub writes the six legitimate ones with
        # this same tool.
        "name": "fp1 iv c forged chain receipt under the dual-lane carve-out is unchanged",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{RECEIPTS}/execution-control-forged.json",
            "content": RECEIPT_LONG,
        },
        "expect": "ALLOW",
    },
    {
        "name": "fp1 iv d Write rewrites an existing pen at its own length",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{DUAL}/orchestrator-resume-CURRENT.md",
            "content": CHECKPOINT_LONG,
        },
        "expect": "ALLOW",
        "fixture": "checkpoint_unarchived",
    },
    {
        "name": "fp1 iv d CTL the same write shrunk to a stub",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{DUAL}/orchestrator-resume-CURRENT.md",
            "content": "# stub\n",
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "shrinking overwrite",
        "fixture": "checkpoint_unarchived",
    },
    # (v) THE NARROWING, PINNED AS A ROW RATHER THAN LEFT AS AN ABSENCE.
    #
    # An earlier revision of this packet admitted a THIRD NA-7 root -- the fleet doctrine bus
    # clone beside this board -- and every one of the four reproducible bypasses the review
    # rounds found lived in that root's machinery.  It was NARROWED OUT.  A removal that is
    # only an absence is indistinguishable from a feature nobody wrote, so the DENY is
    # asserted here: a `Write` to that sibling repository is refused by the ORDINARY NA-7
    # rule, exactly as it was before this branch existed.  `{OUTSIDE}` is the tmp stand-in
    # for any sibling of the board, so the row carries no machine layout.
    #
    # WHAT THIS ROW IS NOT.  It is not a ban on publishing the board's spec: that is a git
    # operation performed by the orchestrator IN the bus repository and reviewed there as a
    # commit, which this gate never sees and does not need to.  See the hook's header.
    {
        "name": "fp1 v Write publishing the board spec to a sibling repository is refused",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{OUTSIDE}/specs/mlv-app.md", "content": "# mlv-app spec\n"},
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "outside both the worktree and the board root",
    },
    {
        "name": "fp1 v CTL the same publish under the board root",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/specs/mlv-app.md", "content": "# mlv-app spec\n"},
        "expect": "ALLOW",
    },
    # (vi) A BIT BUCKET IS A SINK, NOT A FILE.  `/dev/null` is absolute and under neither
    # root, so NA-7 refused it as a write outside both -- which is what made every
    # `2>/dev/null` on this board a denial.  VARIES EXACTLY ONE TOKEN: the destination.
    # (Round 1's only genuinely one-token pair, kept verbatim.)
    {
        "name": "fp1 vi shell write to the bit bucket",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "echo probe > /dev/null"},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 vi CTL shell write to an absolute path outside every root",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "echo probe > \"{OUTSIDE}/stray.txt\""},
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "outside both the worktree and the board root",
    },
    # (vii) S133 -- `..` IS COLLAPSED BEFORE EVERY ROOT AND PREFIX COMPARISON.
    #
    # SOL'S BLOCKER ON PR #104, REPRODUCED HERE.  `norm` folded case and separators and
    # never collapsed `..`, so `<bus>/../escaped.txt` kept the bus root as a literal PREFIX
    # and `under()` said True -- a `Write` anywhere on the drive was ALLOWED by a rule whose
    # whole job is to bound the roots.  A round-1 hook goes RED on every DENY row below.
    # The fix lives in `norm` itself rather than at the NA-7 call site, so the SAME collapse
    # protects NA-2's protected tails and NA-10's gate tails; the last pair proves that.
    #
    # Each control VARIES EXACTLY ONE TOKEN against the subject above it: the path.  All
    # three roots are covered, because a fix applied at one call site would leave the other
    # two open and every other row would still be green.
    {
        "name": "fp1 vii a CTL board publish path walked two levels out with dot dot",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "{BOARD}/specs/../../escaped.txt",
            "content": "# mlv-app spec\n",
        },
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "outside both the worktree and the board root",
    },
    {
        "name": "fp1 vii b Write under the board root",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/scratch/out.txt", "content": "probe\n"},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 vii b CTL the same write walked out of the board root",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/../escaped.txt", "content": "probe\n"},
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "outside both the worktree and the board root",
    },
    {
        # `{REPO}` is the hook's OWN worktree root, derived from `__file__` -- the third
        # root NA-7 admits, and the one a lane actually writes in.
        "name": "fp1 vii c Write under the hook's own worktree root",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{REPO}/build/fp1-scratch.txt", "content": "probe\n"},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 vii c CTL the same write walked out of the worktree root",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "{REPO}/../escaped.txt", "content": "probe\n"},
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "outside both the worktree and the board root",
    },
    {
        # THE OTHER DIRECTION, AND THE REASON THE COLLAPSE BELONGS IN `norm`: `..` must not
        # become a way to HIDE a protected path from NA-2 either.  The ALLOW here is what
        # keeps the fix from degenerating into "deny anything containing `..`" -- the same
        # detour on an unprotected path is still an ordinary write.
        #
        # MEASURED, AND THE MEASUREMENT IS THE POINT.  The round-1 hook DENIED the control
        # below -- but with `truncate of ledger/pen/receipt/evidence content at
        # .../dual-lane/../dual-lane/queue.json`, the GENERIC arm, because `after_seg` could
        # not see the `..` form as `queue.json` and `_carve_tag` returned None.  A right
        # answer from the wrong branch: the carve-out that governs that file was never
        # consulted, so nothing about its shrink rule, its delete rule or its receipts
        # neighbours applied.  `reason_contains` is what makes this row a falsifier rather
        # than a coincidence, and it is why it is pinned to the carve-out's wording.
        "name": "fp1 vii d shell truncation of an unprotected path via a dot dot detour",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "echo x > \"{BOARD}/scratch/../scratch/out.txt\""},
        "expect": "ALLOW",
    },
    {
        "name": "fp1 vii d CTL the same detour onto the protected ledger",
        "group": "falsepositive1",
        "tool": "Bash",
        "input": {"command": "echo x > \"{DUAL}/../dual-lane/queue.json\""},
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": "cannot be proven non-shrinking",
    },
    # (viii) S134 -- THE HOOK'S OWN ENVIRONMENT VARIABLES ARE EXPANDED BEFORE EVERY PATH
    # CHECK, AND THE CLAIM IS NOW MADE ABOUT THE ROOT THAT SURVIVED.
    #
    # The measured defect: a delete of the protected queue ledger spelled through
    # `$env:MLV_BOARD_ROOT` was ALLOWED while the identical LITERAL path was DENIED.  The fix
    # is in `norm`, beside the `..` collapse and BEFORE it, so every arm inherits it and an
    # expanded path is canonicalised rather than compared raw.  This half of the packet is
    # KEPT: it closed a real ALLOW on a protected pen, and it never depended on the
    # sibling-repository root that was narrowed out.
    #
    # WHAT THE PAIR VARIES.  viii IS NOT A ONE-TOKEN DENY/ALLOW PAIR IN THE USUAL SENSE and
    # its control says so: it varies one DIRECTORY SEGMENT (`coordination` ->
    # `coordination-scratch`), which is what takes the path off NA-2's protected tail.  Its
    # falsifying power is in `reason_contains`, not in the verdict: NA-2's tails are matched
    # SEGMENT-WISE, so a hook that does not expand DENIES this row too -- naming the
    # UNEXPANDED `$env:mlv_board_root/...` in the reason.  Pinning the needle to
    # `{BOARD_NORM}` makes the row RED there and is the only way this arm's reach is
    # measurable at all.
    {
        "name": "fp1 viii env spelled delete of the protected queue ledger",
        "group": "falsepositive1",
        "tool": "PowerShell",
        "input": {
            "command": "Remove-Item -LiteralPath "
            '"$env:MLV_BOARD_ROOT/.claude-state/coordination/dual-lane/queue.json"'
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "{BOARD_NORM}/.claude-state/coordination/dual-lane/queue.json",
            "denied on the carve-out paths",
        ),
    },
    {
        "name": "fp1 viii CTL the same variable on an unprotected sibling directory",
        "group": "falsepositive1",
        "tool": "PowerShell",
        "input": {
            "command": "Remove-Item -LiteralPath "
            '"$env:MLV_BOARD_ROOT/.claude-state/coordination-scratch/dual-lane/queue.json"'
        },
        "expect": "ALLOW",
    },
    # (ix) THE SAME ACT WITH THE STEERING VARIABLE UNSET, WHICH IS THE ONLY CONFIGURATION
    # THAT ACTUALLY SHIPS.
    #
    # Substituting a hook-read name "only when the name is actually present in this process's
    # environment" is green in the SET configuration and open in the other: nothing on this
    # machine exports a steering root except this suite, so the guard held only on the
    # machine the falsifier suite builds.  The repair is in `hook_env_values`: a name resolves
    # to the value the hook IS USING -- the override when set, the BUILT-IN DEFAULT when not.
    #
    # This row is the twin of `viii` above, varying EXACTLY ONE THING: whether the name is
    # bound.  Both must DENY, because the hook knows where the board is in both -- the defect
    # was never ignorance of the root, it was asking "is the variable set?" instead of "what
    # value am I using?".  The needle pins the RESOLVED DEFAULT (`{DEFAULT_BOARD_NORM}`,
    # derived in `setUp` from the hook's own constant), so a hook that regressed to leaving
    # the reference literal fails by verdict AND by name.
    {
        "name": "fp1 ix env spelled delete of the queue ledger with the board root UNSET",
        "group": "falsepositive1",
        "tool": "PowerShell",
        "input": {
            "command": "Remove-Item -LiteralPath "
            '"$env:MLV_BOARD_ROOT/.claude-state/coordination/dual-lane/queue.json"'
        },
        "expect": "DENY",
        "na": "NA-2",
        "reason_contains": (
            "{DEFAULT_BOARD_NORM}/.claude-state/coordination/dual-lane/queue.json",
            "denied on the carve-out paths",
        ),
        "fixture": "board_root_unset",
    },
    # (x) AN UNRESOLVABLE ENVIRONMENT REFERENCE IS A DENIAL, NEVER A COLLAPSE.
    #
    # THE ROUND-5 REVIEWER'S REPRO, AND IT SURVIVES THE NARROWING BECAUSE IT WAS NEVER ABOUT
    # THE REMOVED ROOT.  `MLV_LANE_PROMPT` is a name this hook READS but which has NO default,
    # so when it is unset the reference is deliberately left standing as literal text (see
    # `hook_env_values`).  `norm` then canonicalised `..` against that text and
    # `"$env:MLV_LANE_PROMPT/../outside.txt"` became the bare relative `outside.txt` -- which
    # NA-7's early return waves through as "resolves inside the worktree by construction",
    # while the SHELL resolves the same string to an absolute path outside every root.  ALLOW,
    # measured on `10b71482`.  `norm` now declines to collapse a path that still carries a
    # reference and NA-7 refuses it as UNRESOLVABLE.
    #
    # THE PAIR VARIES EXACTLY ONE TOKEN: the leading segment.  The control is the identical
    # `../outside.txt` walk with no reference in front of it, and it must keep WHATEVER
    # behaviour it had -- which is DENY, from the `..`-out-of-the-worktree arm S133 added.
    # That is the point of pairing them: the new rule must not be doing the old rule's job,
    # and the two refusals are attributable by their DIFFERENT reasons.
    {
        "name": "fp1 x an unresolvable env reference walks out and is refused",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {
            "file_path": "$env:MLV_LANE_PROMPT/../outside.txt",
            "content": "probe\n",
        },
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "UNRESOLVABLE environment reference",
        "fixture": "lane_prompt_unset",
    },
    {
        "name": "fp1 x CTL the same walk with an ordinary relative path and no reference",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "prompts/../outside.txt", "content": "probe\n"},
        "expect": "ALLOW",
        "fixture": "lane_prompt_unset",
    },
    {
        "name": "fp1 x CTL a bare dot dot walk with no reference keeps its own refusal",
        "group": "falsepositive1",
        "tool": "Write",
        "input": {"file_path": "../outside.txt", "content": "probe\n"},
        "expect": "DENY",
        "na": "NA-7",
        "reason_contains": "walks out of the worktree with `..`",
        "fixture": "lane_prompt_unset",
    },
    # ------------------------------------------------------- the 6 benign ALLOW controls
    {
        "name": "benign git push fork product branch",
        "group": "benign",
        "tool": "Bash",
        "input": {"command": "git push fork product/x"},
        "expect": "ALLOW",
    },
    {
        "name": "benign git rm --cached a profiling dng",
        "group": "benign",
        "tool": "Bash",
        "input": {"command": "git rm --cached .claude/profiling/a.dng"},
        "expect": "ALLOW",
    },
    {
        "name": "benign rm build objects",
        "group": "benign",
        "tool": "Bash",
        "input": {"command": "rm build/*.o"},
        "expect": "ALLOW",
    },
    {
        "name": "benign Write a new console test with assertions",
        "group": "benign",
        "tool": "Write",
        "input": {"file_path": "{BOARD}/tests/console/new_test.cpp", "content": TEST_BEFORE},
        "expect": "ALLOW",
    },
    {
        "name": "benign gh pr create",
        "group": "benign",
        "tool": "Bash",
        "input": {"command": "gh pr create --title 'hook' --body 'lands the project hook'"},
        "expect": "ALLOW",
    },
    {
        "name": "benign add-only required-checks patch with the snapshot present",
        "group": "benign",
        "tool": "Bash",
        "input": {"command": ADD_ONLY_PATCH},
        "expect": "ALLOW",
        "fixture": "snapshot",
    },
]


# ----------------------------------------------------------------------- fixtures
#
# A fixture returns ENVIRONMENT overrides, plus -- optionally -- this ONE key, which is not
# an environment variable at all: `_invoke` pops it and turns it into the hook's
# `--project-dir` ARGUMENT (O126).  Keeping it out of the environment is the point: the
# venue must reach the hook the way the registration delivers it, and a row that wants to
# prove the environment is NOT read sets `CLAUDE_PROJECT_DIR` instead and passes no argument.
VENUE_KEY = "__project_dir__"


class _Unset(object):
    """ROUND 5: a fixture value meaning DELETE THIS NAME FROM THE HOOK'S ENVIRONMENT.

    THE CAPABILITY THIS SUITE DID NOT HAVE, AND ITS ABSENCE IS WHAT LET A HOLE THROUGH.
    `_invoke` binds `MLV_BOARD_ROOT`, `MLV_CLIP_CACHE_ROOT` and
    `MLV_REQUIRED_CHECKS_SNAPSHOT` for EVERY row, so until this delta the suite could only
    ever measure the SET configuration.  An expansion that substituted a name only when it
    was PRESENT is green in that configuration and open in the other: with the name unset --
    the AMBIENT state on this machine -- the env-spelled delete of a protected path was
    ALLOWED while the literal one was DENIED.  A steering root's DEFAULT is a configuration
    this hook actually runs in (it is the only one that ships), so it has to be a
    configuration this table can express.

    An override of `UNSET` is popped from the row's overrides AND deleted from the hook's
    environment, so the hook falls back to its OWN built-in default -- which is what the
    paired rows below measure.  Nothing is executed and no path is stat'ed; the rows assert a
    DECISION about a path, so binding a name to nothing cannot touch this machine.
    """

    def __repr__(self):
        return "UNSET"


UNSET = _Unset()


def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _snapshot_rows(paths, tail_is_malformed=False):
    rows = [json.dumps({"recordedUtc": "2026-09-06T05:33:00Z", "required_status_checks": _body(LIVE_CONTEXTS)})]
    if tail_is_malformed:
        rows.append('{"required_status_checks": {"strict": true, "checks": [')
    return "\n".join(rows) + "\n"


# ------------------------------------------- S118: the receipts CONFORM to the schema table
#
# Until this delta the receipt fixtures were placeholders -- `{"step": "0.18", "parity":
# true}` and friends -- because "validate" meant `json.load` did not return `None`.  The hub
# reproduced what that was worth on the sixth commit: five `{}` receipts beside an
# `execution-control-forged.json` carrying only the provenance keys ALLOWED the one-shot
# enable.  So every ALLOW row's receipt set is now FULLY CONFORMING, and -- this is the part
# that matters -- every bound value is COMPUTED AT TEST TIME from the fixture itself:
#
#   * every `path` field names a file this fixture WROTE, so the hook's existence test is
#     answered by the tmp board and never by the host;
#   * every `sha256` is a real digest -- of a file on disk where the schema binds one, and
#     of a derived label where the schema asks only for the shape;
#   * `roadmapParityReceiptSha256` is the sha256 of `0.18-roadmap-parity.json` AS WRITTEN,
#     read back from disk after the write, so a newline translation or an indent change
#     cannot make the ALLOW rows pass against a hook that stopped hashing the file.
#
# Nothing here is a hard-coded digest.  A table that wrote one down would go green against a
# hook that had stopped opening the file -- which is the S112/S118 defect in its purest form.
RECEIPT_STAMP = "2026-09-06T08:00:00Z"
RECEIPT_RUN_URL = "https://github.com/layibabalola/MLV-App/actions/runs/17512345678"
# The UPSTREAM remote, and the reason `url` is a prefix test rather than a "looks like a
# GitHub URL" test: the board carries two remotes and an unpinned `gh` resolves to this one
# (O135), so a run URL on this host is the value the class exists to refuse.
RECEIPT_RUN_URL_WRONG_HOST = "https://github.com/ilia3101/MLV-App/actions/runs/17512345678"
# `path` fields resolve ABSOLUTE first, then relative to the BOARD root, so the fixture
# writes these under the tmp board and stores the RELATIVE form -- the same shape the plan's
# steps record, and identical on both matrix legs.
EVIDENCE_DIR = ".claude-state/coordination/dual-lane/evidence"
EVIDENCE_FILES = {
    "composedPromptPath": EVIDENCE_DIR + "/sol-review-PR-74.md",
    "prChecksPath": EVIDENCE_DIR + "/pr-74-checks.json",
    "prReviewPath": EVIDENCE_DIR + "/pr-74-review.json",
    "solVerdictPath": EVIDENCE_DIR + "/sol-verdict.md",
    "dryRunDiff": EVIDENCE_DIR + "/factory-freeze-dry-run.diff",
    "queue": ".claude-state/coordination/dual-lane/queue.json",
}
MISSING_EVIDENCE_PATH = EVIDENCE_DIR + "/no-such-composed-prompt.md"

# The six chain names, as STEPS -- the enumeration the hook restricts the candidate set to.
CHAIN_STEPS = ("0.1", "0.35", "0.4c-i", "0.4b-i", "0.6", "0.7")
CHAIN_PROVENANCE_EXEMPT = ("0.1",)
FORGED_CONTROL_STEP = "forged"
# The default two-receipt chain, and the stamps the pre-S118 fixture already used.
CONTROL_STAMP_06 = "2026-09-06T09:00:00Z"
CONTROL_STAMP_07 = "2026-09-06T11:00:00Z"
# O177: a THREE-receipt chain {0.4b-i, 0.6, 0.7} so an O152 repair of 0.6 has a stamp either
# side of it to sit between -- 0.4b-i's parsed stamp below it, a valid 0.7's above it.  The
# ALLOW pair is order-preserving (0.4b-i < 0.6-repaired < 0.7); the DENY pair restamps the
# SAME repair at the moment of the write, i.e. later than 0.7's, and nothing else changes.
CONTROL_STAMP_O177_04B_I = "2026-09-06T08:00:00Z"
CONTROL_STAMP_O177_06_REPAIRED_BETWEEN = "2026-09-06T10:00:00Z"
CONTROL_STAMP_O177_07 = "2026-09-06T12:00:00Z"
CONTROL_STAMP_O177_06_REPAIRED_AT_WRITE = "2026-09-06T13:00:00Z"
# `roadmapParityReceiptSha256` is bound to bytes that do not exist until the fixture writes
# them, so the payload carries a TOKEN and `_kill_switch_receipts` substitutes the real
# digest at write time.  A row that wants a WRONG digest passes an explicit value instead,
# which the substitution then leaves alone.
PARITY_SHA_TOKEN = "{PARITY_SHA256}"
_DROP = object()

# ------------------------------------------- S120/O158: the fixed hash sets and the verdicts
#
# THE SUITE'S OWN RESTATEMENT OF PLAN 1.3 STEP 5's TABLE, spelled out in FULL rather than
# derived from a base plus increments the way the hook writes it, so the two are independent
# readings of the plan that the six per-step ALLOW rows and the missing/extra-key DENY rows
# force to agree.  A path here is the plan's: forward-slashed, board-relative.
REQUIRED_HASHES = {
    "0.1": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
    ),
    "0.35": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
        "tools/coordination/Compose-LanePrompt.ps1",
    ),
    "0.4c-i": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
        "tools/coordination/Compose-LanePrompt.ps1",
        "tools/coordination/demote-factory-bridge.ps1",
    ),
    "0.4b-i": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
        "tools/coordination/Compose-LanePrompt.ps1",
        "tools/coordination/demote-factory-bridge.ps1",
        "tools/coordination/set-required-checks.ps1",
    ),
    "0.6": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
        "tools/coordination/Compose-LanePrompt.ps1",
        "tools/coordination/demote-factory-bridge.ps1",
        "tools/coordination/set-required-checks.ps1",
        "tools/coordination/Test-ProductRatioGuard.ps1",
        "tools/coordination/freeze-factory-cards.py",
    ),
    "0.7": (
        "tools/hooks/mlv-never-authorized.py",
        "tools/repo_hygiene/test_mlv_never_authorized.py",
        "tools/hooks/test_registration_path_local.py",
        "tools/coordination/Invoke-Lane.ps1",
        "tools/coordination/Invoke-Workstream.ps1",
        "tools/coordination/Invoke-WorkstreamLoop.ps1",
        "tools/coordination/Compose-LanePrompt.ps1",
        "tools/coordination/demote-factory-bridge.ps1",
        "tools/coordination/set-required-checks.ps1",
        "tools/coordination/Test-ProductRatioGuard.ps1",
        "tools/coordination/freeze-factory-cards.py",
    ),
}
# Every path any step hashes -- the files the fixture writes under the tmp board so the O158
# re-hash has a board to walk.  A forged step (`FORGED_CONTROL_STEP`) borrows 0.7's set:
# the forged receipt is refused for its NAME and must be plausible in every other respect.
ALL_HASHED_PATHS = tuple(
    sorted(set(path for paths in REQUIRED_HASHES.values() for path in paths))
)
# The S120 key-set near misses: a BASE path (in every step's set) dropped from 0.6, and a
# path no step landed added to 0.7.  The O158 board mutations: a BASE file edited after the
# receipt was written, and a 0.6/0.7-only file deleted.
HASHES_DROPPED_PATH = "tools/coordination/Invoke-Lane.ps1"
HASHES_EXTRA_PATH = "tools/hooks/extra-script.py"
REHASH_DRIFTED_PATH = "tools/coordination/Invoke-Lane.ps1"
REHASH_MISSING_PATH = "tools/coordination/freeze-factory-cards.py"
# The per-step keys beyond the common ones (S120): 0.1 records that the composer does not
# exist yet, beside the three evidence paths; 0.35 lands it and records the three paths.
CHAIN_COMPOSER_STATUS_STEPS = ("0.1",)
CHAIN_COMPOSER_PATH_STEPS = ("0.1", "0.35")
COMPOSER_STATUS_NOT_YET_CREATED = "not-yet-created"
COMPOSER_PATH_KEYS = ("composedPromptPath", "prChecksPath", "prReviewPath")
# The second key's verdicts (S120).  One APPROVE per step at that step's derived head, in
# sol's template shape -- prose ending in a ```json fence -- except 0.35's, saved as plain
# JSON so the ALLOW rows exercise the "last top-level object" reading too; and one
# CHANGES_REQUESTED at 0.7's head for the verdict-arm row.
VERDICT_DIR = EVIDENCE_DIR + "/sol-verdicts"
VERDICT_APPROVE = "APPROVE"
VERDICT_CHANGES_REQUESTED = "CHANGES_REQUESTED"
VERDICT_PLAIN_JSON_STEP = "0.35"


def _derived_sha256(label):
    """A 64-char lowercase sha256 that is COMPUTED, for the fields the schema shape-checks."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _derived_sha(label):
    """A 40-char lowercase hex sha -- a git object name's shape, computed, never written down."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:40]


def _sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _evidence_path(paths, label):
    return os.path.join(paths["BOARD"], EVIDENCE_FILES[label].replace("/", os.sep))


def _write_evidence(paths):
    """Write every file a `path` field names, so the hook's existence test has something to find."""
    for label in sorted(EVIDENCE_FILES):
        _write(_evidence_path(paths, label), "evidence fixture: %s\n" % label)


def _hashed_file_path(paths, relative):
    return os.path.join(paths["BOARD"], relative.replace("/", os.sep))


def _write_hashed_files(paths):
    """Write every file any step hashes under the tmp board, so the O158 re-hash has a board."""
    for relative in ALL_HASHED_PATHS:
        _write(_hashed_file_path(paths, relative), "fixed-set fixture: %s\n" % relative)


def _board_hashes(paths, step):
    """The step's REQUIRED set mapped to the REAL sha256 of each file on the tmp board (S120/O158)."""
    _write_hashed_files(paths)
    required = REQUIRED_HASHES.get(step, REQUIRED_HASHES["0.7"])
    return dict(
        (relative, _sha256_file(_hashed_file_path(paths, relative))) for relative in required
    )


def _head_sha(step):
    """The PR HEAD sol reviewed for a step BEFORE the merge -- derived, 40 lowercase hex."""
    return _derived_sha("head:" + step)


def _merge_sha(step):
    """The post-merge commit a step's hashes were taken at -- derived, and NOT the head (S123).

    A GitHub merge lands a DIFFERENT commit from the reviewed PR head, so the conforming
    fixture carries two different shas and writes the verdict at the FIRST;
    `test_every_chain_receipt_the_fixture_writes_carries_two_different_shas_...` asserts the
    two never coincide by accident.
    """
    return _derived_sha("merge:" + step)


def _fixed_set_digest(hashes):
    """The SUITE's own canonicalisation of a `hashes` object (O172) -- deliberately a second implementation.

    The `<sha256>  <path>` lines (two spaces), sorted as plain strings, LF-joined with no
    trailing newline, UTF-8, sha256, lowercase hex.  It does not import the hook's, so the
    ALLOW rows compare two readings of the plan's sentence rather than one reading with
    itself -- and `test_every_chain_receipt_the_fixture_writes_carries_a_proof_...` writes
    the canonicalisation out a THIRD time, inline, against both.
    """
    lines = sorted("%s  %s" % (digest, path) for path, digest in hashes.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _equality_proof(reviewed, merge, hashes):
    """`<reviewedHeadSha>=<mergeSha>:<digest>` -- the literal the hub writes after its git assertion (O172)."""
    return "%s=%s:%s" % (reviewed, merge, _fixed_set_digest(hashes))


def _chain_stamp(step):
    """A conforming stamp that ASCENDS with the chain, so a chain fixture is never a tie."""
    return "2026-09-06T1%d:00:00Z" % CHAIN_STEPS.index(step)


def _verdict_relative(step, verdict=VERDICT_APPROVE):
    """The board-relative path of the fixture's verdict file for one step and one verdict."""
    leaf = "sol-review-%s" % step
    if verdict != VERDICT_APPROVE:
        leaf += "-" + verdict.lower()
    suffix = ".json" if step == VERDICT_PLAIN_JSON_STEP else ".md"
    return VERDICT_DIR + "/" + leaf + suffix


def _verdict_text(step, verdict, subject):
    """A verdict file in sol's template shape, or as plain JSON for the plain-JSON step.

    The markdown form carries an EARLIER fenced block with a BLOCKER at a digest of nothing,
    so a hook that read the FIRST fence -- or any fence but the last -- goes red on every
    ALLOW row rather than passing for the wrong reason.
    """
    block = json.dumps(
        {
            "verdict": verdict,
            "subject_sha": subject,
            "pr": 74,
            "findings": [],
            "self_failure": "UNMEASURED: the pilot's wall-clock number was not re-run",
        },
        indent=1,
    )
    if step == VERDICT_PLAIN_JSON_STEP:
        return block + "\n"
    earlier = json.dumps({"verdict": "BLOCKER", "subject_sha": "0" * 40, "pr": 74})
    return (
        "# sol review of PR 74 for step %s\n\n"
        "An earlier draft of this review ended with the block below; it is quoted here so the\n"
        "record shows what was retracted, and it is NOT the terminal block:\n\n"
        "```json\n%s\n```\n\n"
        "## Output\n\n"
        "```json\n%s\n```\n" % (step, earlier, block)
    )


def _write_verdicts(paths):
    """Write every verdict a fixture's `solVerdictPath` may name (S120)."""
    for step in CHAIN_STEPS + (FORGED_CONTROL_STEP,):
        _write(
            os.path.join(paths["BOARD"], _verdict_relative(step).replace("/", os.sep)),
            _verdict_text(step, VERDICT_APPROVE, _head_sha(step)),
        )
    _write(
        os.path.join(
            paths["BOARD"],
            _verdict_relative("0.7", VERDICT_CHANGES_REQUESTED).replace("/", os.sep),
        ),
        _verdict_text("0.7", VERDICT_CHANGES_REQUESTED, _head_sha("0.7")),
    )


def _receipt_payloads(paths):
    """The five FIXED gate receipts as documents, fully conforming to the schema table."""
    _write_evidence(paths)
    snapshot_row = json.dumps(
        {
            "recordedUtc": RECEIPT_STAMP,
            "required_status_checks": _body(CANONICAL_04B_CONTEXTS),
        }
    )
    queue_sha = _sha256_file(_evidence_path(paths, "queue"))
    return {
        "0.18-roadmap-parity.json": {
            "step": "0.18",
            "recordedUtc": RECEIPT_STAMP,
            "queueArmResultSha256": queue_sha,
            "composedPromptPath": EVIDENCE_FILES["composedPromptPath"],
            "prChecksPath": EVIDENCE_FILES["prChecksPath"],
            "prReviewPath": EVIDENCE_FILES["prReviewPath"],
            "solVerdictPath": EVIDENCE_FILES["solVerdictPath"],
        },
        "0.4b-required-checks.json": {
            "step": "0.4b",
            "recordedUtc": RECEIPT_STAMP,
            "headSha": _derived_sha("0.4b-head"),
            "preContexts": list(LIVE_CONTEXTS),
            "postContexts": list(CANONICAL_04B_CONTEXTS),
            "snapshotRowSha256": hashlib.sha256(
                snapshot_row.encode("utf-8")
            ).hexdigest(),
        },
        "0.4c-demoted.json": {
            "step": "0.4c",
            "recordedUtc": RECEIPT_STAMP,
            "headSha": _derived_sha("0.4c-head"),
            "mergeSha": _derived_sha("0.4c-merge"),
            "runUrl": RECEIPT_RUN_URL,
            "solVerdictPath": EVIDENCE_FILES["solVerdictPath"],
        },
        "0.6-ratio-guard.json": {
            "step": "0.6",
            "recordedUtc": RECEIPT_STAMP,
            "mergeSha": _derived_sha("0.6-merge"),
            "firstReading": {"dispatched": 0, "charged": 0, "window": "24h"},
            "solVerdictPath": EVIDENCE_FILES["solVerdictPath"],
        },
        "0.5-factory-frozen.json": {
            "step": "0.5",
            "recordedUtc": RECEIPT_STAMP,
            "queueSha256": queue_sha,
            "frozenCount": 12,
            "dryRunDiffSha256": _sha256_file(_evidence_path(paths, "dryRunDiff")),
            "scopelessIds": ["FACT-11", "FACT-12"],
        },
    }


FIXED_RECEIPT_ORDER = (
    "0.18-roadmap-parity.json",
    "0.4b-required-checks.json",
    "0.4c-demoted.json",
    "0.6-ratio-guard.json",
    "0.5-factory-frozen.json",
)
ROADMAP_PARITY_RECEIPT = FIXED_RECEIPT_ORDER[0]


def _mutated(paths, name, **changes):
    """The CONFORMING payload for one fixed receipt, with named keys set or dropped.

    Every S118 fixed-receipt falsifier is built this way, so it differs from the ALLOW row's
    receipt in EXACTLY the key it names and in nothing else -- the same discipline the S101
    and S112 rows already use on the compound and on the literal.
    """
    document = _receipt_payloads(paths)[name]
    for key, value in changes.items():
        if value is _DROP:
            document.pop(key, None)
        else:
            document[key] = value
    return json.dumps(document, indent=2)


def _control_name(step):
    return "execution-control-%s.json" % step


def _execution_control(
    paths,
    step,
    stamp,
    provenance=True,
    hashes=None,
    parity=PARITY_SHA_TOKEN,
    queue_sha=None,
    product_live_count=15,
    reviewed_head=None,
    merge_sha=None,
    verdict_path=None,
    equality_proof=None,
    drop=(),
):
    """One chain receipt.  Every keyword is ONE degree of freedom of the chain schema.

    The defaults are the CONFORMING receipt for `step`, computed from the tmp board (S120):
    `reviewedHeadSha` is the step's derived REVIEWED head, `mergeSha` its derived -- and
    DIFFERENT -- post-merge commit (S123), and `solVerdictPath` the fixture's APPROVE of
    exactly the reviewed head; `hashes` is the step's fixed set mapped to the REAL digest of
    each file under the tmp board (O158); `fixedSetEqualityProof` is DERIVED from the two
    shas and the `hashes` the receipt carries, mutated or not (O172); 0.1 carries
    `composerStatus` and the three evidence paths, 0.35 the three paths.  A row that wants a
    near miss passes ONE keyword.
    """
    _write_evidence(paths)
    _write_verdicts(paths)
    document = {"step": step, "recordedUtc": stamp}
    if stamp is None:
        del document["recordedUtc"]
    document["reviewedHeadSha"] = (
        reviewed_head if reviewed_head is not None else _head_sha(step)
    )
    document["mergeSha"] = merge_sha if merge_sha is not None else _merge_sha(step)
    document["solVerdictPath"] = (
        verdict_path if verdict_path is not None else _verdict_relative(step)
    )
    document["hashes"] = dict(hashes) if hashes is not None else _board_hashes(paths, step)
    # O172: DERIVED from this receipt's own two shas and its own `hashes` -- the ones just
    # set, mutated or not -- so a near-miss row that drops a hashed path carries a proof of
    # the SET IT CARRIES, and its refusal is the key-set arm's, never the proof's.
    document["fixedSetEqualityProof"] = (
        equality_proof
        if equality_proof is not None
        else _equality_proof(
            document["reviewedHeadSha"], document["mergeSha"], document["hashes"]
        )
    )
    if step in CHAIN_COMPOSER_STATUS_STEPS:
        document["composerStatus"] = COMPOSER_STATUS_NOT_YET_CREATED
    if step in CHAIN_COMPOSER_PATH_STEPS:
        for key in COMPOSER_PATH_KEYS:
            document[key] = EVIDENCE_FILES[key]
    if provenance:
        document["roadmapParityReceiptSha256"] = parity
        document["queueSha256"] = (
            queue_sha if queue_sha is not None else _derived_sha256("queue:" + step)
        )
        document["productLiveCount"] = product_live_count
    for key in drop:
        document.pop(key, None)
    return json.dumps(document, indent=2)


def _kill_switch_receipts(paths, omit=(), extra=(), stamps=None):
    """The gate receipts, fully conforming unless a row deliberately breaks one (S118).

    `stamps` is an ORDERED mapping of chain STEP -> `recordedUtc`, and its keys ARE the chain
    this fixture writes: the default is the `{0.6, 0.7}` pair the pre-S118 fixture used, an
    O141 row passes two stamps, and an S118 row passes a longer or shorter chain to exercise
    the selection and the "selected receipt absent" arm.

    `extra` still OVERWRITES by basename -- a chain name replaces that receipt in place, any
    other name is written beside the chain (which is how the forged-name row is built).  The
    five fixed receipts are written FIRST and `0.18-roadmap-parity.json` is then hashed OFF
    DISK, so every chain receipt's `roadmapParityReceiptSha256` token resolves to the digest
    of the bytes the hook will read.
    """
    receipts = paths["RECEIPTS"]
    overrides = dict(extra)
    conforming = _receipt_payloads(paths)
    for name in FIXED_RECEIPT_ORDER:
        if name in omit:
            continue
        payload = (
            overrides.pop(name)
            if name in overrides
            else json.dumps(conforming[name], indent=2)
        )
        _write(os.path.join(receipts, name), payload)
    parity_path = os.path.join(receipts, ROADMAP_PARITY_RECEIPT)
    parity_sha = _sha256_file(parity_path) if os.path.isfile(parity_path) else "0" * 64
    if stamps is None:
        stamps = {"0.6": CONTROL_STAMP_06, "0.7": CONTROL_STAMP_07}
    for step, stamp in stamps.items():
        name = _control_name(step)
        if name in omit:
            continue
        payload = (
            overrides.pop(name)
            if name in overrides
            else _execution_control(
                paths, step, stamp, provenance=step not in CHAIN_PROVENANCE_EXEMPT
            )
        )
        _write(os.path.join(receipts, name), payload.replace(PARITY_SHA_TOKEN, parity_sha))
    for name in sorted(overrides):
        if name in omit:
            continue
        _write(
            os.path.join(receipts, name),
            overrides[name].replace(PARITY_SHA_TOKEN, parity_sha),
        )


def _kill_switch_receipts_stamped(paths, stamp_06, stamp_07):
    """The six receipts, with the two execution-control stamps set explicitly (O141).

    Each O141 fixture differs from `fixture_receipts_all_six_at_board` in the stamps and in
    nothing else.
    """
    _kill_switch_receipts(paths, stamps={"0.6": stamp_06, "0.7": stamp_07})


def fixture_default(paths):
    return {}


def fixture_snapshot(paths):
    _write(paths["SNAPSHOT"], _snapshot_rows(paths))
    return {}


def fixture_snapshot_malformed_tail(paths):
    _write(paths["SNAPSHOT"], _snapshot_rows(paths, tail_is_malformed=True))
    return {}


def fixture_snapshot_absent(paths):
    return {"MLV_REQUIRED_CHECKS_SNAPSHOT": os.path.join(paths["BOARD"], "no-such-snapshot.jsonl")}


def fixture_clip_authorized(paths):
    prompt = os.path.join(paths["BOARD"], "prompt.md")
    _write(prompt, "# card\n\n- CARD_ID: TOOL-HOOK-ENFORCE-1\n- CLIP_OR_NONE: %s\n" % paths["CLIP_AUTH"])
    return {"MLV_LANE_PROMPT": prompt}


def fixture_existing_receipt(paths):
    _write(os.path.join(paths["RECEIPTS"], "live.json"), RECEIPT_LONG)
    return {}


def fixture_existing_ledger(paths):
    """HOOK-FALSE-POSITIVE-1: the gate ledger EXISTS, so a `Write` of it really overwrites.

    The `r2 Write over a coordination ledger` row says `over` and always meant an
    overwrite, but nothing had ever put the file on the tmp board -- so up to this delta it
    was passing against the generic arm's blanket refusal of a path it never opened.  Now
    that an ABSENT target is a create-or-extend, the row states its own precondition.
    """
    _write(os.path.join(paths["DUAL"], "claude.md"), CHECKPOINT_LONG)
    return {}


def fixture_lane_prompt_unset(paths):
    """THE UNRESOLVABLE-REFERENCE ROWS: `MLV_LANE_PROMPT` bound to NOTHING, stated explicitly.

    `_invoke` strips every `MLV_*` name before it binds the three it anchors, so this name is
    already absent on every row -- which is exactly why it is written down here rather than
    relied on.  These rows assert a DECISION about a path spelled through a name the hook
    READS but CANNOT resolve (it has no built-in default), and the precondition that makes
    them mean anything is that the name is unbound.  A future harness change that started
    exporting it would otherwise turn these falsifiers green for the wrong reason.

    `MLV_BOARD_ROOT` stays bound to the tmp board, so the prompt name is the ONE thing this
    fixture varies.  Nothing is written and no path is stat'ed.
    """
    return {"MLV_LANE_PROMPT": UNSET}


def fixture_board_root_unset(paths):
    """ROUND 5: the BOARD root not exported either -- the same question for the other root.

    `ix-e` proved the board root is expanded when it is SET.  This is its unset twin: with
    the name absent the hook judges against `DEFAULT_BOARD_ROOT`, and the NA-2 tail must
    still fire, naming the RESOLVED default rather than the literal `$env:...` text.
    """
    return {"MLV_BOARD_ROOT": UNSET}


def fixture_receipts_all_six(paths):
    """Six receipts, enable unspent -- and NO venue argument (S105's ABSENT reading)."""
    _kill_switch_receipts(paths)
    return {}


def fixture_receipts_all_six_at_board(paths):
    """S105/O126: the same six receipts, at the venue where the enable act is authorized."""
    _kill_switch_receipts(paths)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_receipts_all_six_at_worktree(paths):
    """The same six receipts, at a LANE's venue -- exception (i) does not apply there."""
    _kill_switch_receipts(paths)
    return {VENUE_KEY: paths["WORKTREE"]}


def fixture_receipts_all_six_plus_enable(paths):
    """S98: the six receipts valid AND the ONE-SHOT enable receipt already written."""
    _kill_switch_receipts(paths)
    _write(
        os.path.join(paths["RECEIPTS"], "0.2-loop-enabled.json"),
        json.dumps({"step": "0.2", "recordedUtc": "2026-09-06T12:00:00Z", "enabled": True}),
    )
    return {}


def fixture_preflight_artifact_present_at_board(paths):
    """S125: the six receipts at the board venue, and the pre-flight artifact ALREADY on disk.

    The act CREATES the artifact; a stale one is the hub's to recover, never this act's to
    overwrite -- so the same Write the ALLOW row admits is refused here, naming the target,
    and the content it carries is never read.
    """
    _kill_switch_receipts(paths)
    _write(
        os.path.join(paths["RECEIPTS"], "0.2-enable-preflight-input.json"),
        json.dumps(
            {"tool_name": "PowerShell", "tool_input": {"command": "an earlier pre-flight"}}
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_receipts_missing_one(paths):
    _kill_switch_receipts(paths, omit=("0.6-ratio-guard.json",))
    return {}


def fixture_receipts_no_recorded_utc(paths):
    """O105: a chain receipt with NO `recordedUtc` at all, beside two that carry one.

    S118 moved the receipt this row uses.  It used to be `execution-control-0.5.json`, a
    name outside the chain -- which the candidate-set restriction now refuses BY NAME, so
    the row would have stopped testing O105 and started testing S118's enumeration.  It is
    `0.35` now: a real chain step, refused for the missing stamp and for nothing else.
    """
    _kill_switch_receipts(
        paths,
        stamps={"0.35": None, "0.6": CONTROL_STAMP_06, "0.7": CONTROL_STAMP_07},
    )
    return {}


def fixture_control_stamp_fraction_at_board(paths):
    """O141: the hub's reproduction pair -- a whole second beside a FRACTIONAL stamp."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_FRACTION)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_stamp_offset_at_board(paths):
    """O141: an OFFSET (`+00:00`) where the register pins a trailing `Z`."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_OFFSET)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_stamp_naive_at_board(paths):
    """O141: a NAIVE stamp -- no zone designator at all."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_NAIVE)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_stamp_lowercase_z_at_board(paths):
    """O141: a LOWERCASE `z`, which is a case fold away from the pinned literal."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_LOWER_Z)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_stamps_tied_at_board(paths):
    """O141: two conforming stamps naming the SAME instant -- no newest exists."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_WHOLE)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_stamps_ordered_at_board(paths):
    """O141: two conforming stamps ONE SECOND apart -- `0.7` is the newest, and only it."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_WHOLE, CONTROL_STAMP_LATER)
    return {VENUE_KEY: paths["BOARD"]}


# ------------------------------------------- S118: the receipt SCHEMA fixtures
#
# Every one of these differs from `fixture_receipts_all_six_at_board` in EXACTLY one key of
# one receipt, and all of them run at the BOARD venue, so each row's DENY is attributable to
# the schema arm it names and not to the venue, the shape or the literal.


def _fault_at_board(paths, name, payload):
    """One fixed receipt replaced by a deliberately invalid payload; everything else conforms."""
    _kill_switch_receipts(paths, extra=((name, payload),))
    return {VENUE_KEY: paths["BOARD"]}


def fixture_receipt_018_empty(paths):
    return _fault_at_board(paths, "0.18-roadmap-parity.json", "{}")


def fixture_receipt_04b_empty(paths):
    return _fault_at_board(paths, "0.4b-required-checks.json", "{}")


def fixture_receipt_04c_empty(paths):
    return _fault_at_board(paths, "0.4c-demoted.json", "{}")


def fixture_receipt_06_empty(paths):
    return _fault_at_board(paths, "0.6-ratio-guard.json", "{}")


def fixture_receipt_05_empty(paths):
    return _fault_at_board(paths, "0.5-factory-frozen.json", "{}")


def fixture_receipt_018_missing_key(paths):
    return _fault_at_board(
        paths,
        "0.18-roadmap-parity.json",
        _mutated(paths, "0.18-roadmap-parity.json", queueArmResultSha256=_DROP),
    )


def fixture_receipt_04b_missing_key(paths):
    return _fault_at_board(
        paths,
        "0.4b-required-checks.json",
        _mutated(paths, "0.4b-required-checks.json", headSha=_DROP),
    )


def fixture_receipt_04c_missing_key(paths):
    return _fault_at_board(
        paths, "0.4c-demoted.json", _mutated(paths, "0.4c-demoted.json", mergeSha=_DROP)
    )


def fixture_receipt_06_missing_key(paths):
    return _fault_at_board(
        paths,
        "0.6-ratio-guard.json",
        _mutated(paths, "0.6-ratio-guard.json", firstReading=_DROP),
    )


def fixture_receipt_05_missing_key(paths):
    return _fault_at_board(
        paths,
        "0.5-factory-frozen.json",
        _mutated(paths, "0.5-factory-frozen.json", frozenCount=_DROP),
    )


def fixture_receipt_sha256_uppercase(paths):
    """The RIGHT digest of the RIGHT file, uppercased -- the S112 near miss, on a receipt."""
    conforming = _receipt_payloads(paths)["0.18-roadmap-parity.json"]
    return _fault_at_board(
        paths,
        "0.18-roadmap-parity.json",
        _mutated(
            paths,
            "0.18-roadmap-parity.json",
            queueArmResultSha256=conforming["queueArmResultSha256"].upper(),
        ),
    )


def fixture_receipt_sha256_63_chars(paths):
    """63 of the right 64 characters: a prefix match would take it, and a prefix is not equality."""
    conforming = _receipt_payloads(paths)["0.5-factory-frozen.json"]
    return _fault_at_board(
        paths,
        "0.5-factory-frozen.json",
        _mutated(
            paths,
            "0.5-factory-frozen.json",
            dryRunDiffSha256=conforming["dryRunDiffSha256"][:63],
        ),
    )


def fixture_receipt_path_missing(paths):
    """A `path` field of the right SHAPE naming a file that is not there."""
    return _fault_at_board(
        paths,
        "0.18-roadmap-parity.json",
        _mutated(
            paths, "0.18-roadmap-parity.json", composedPromptPath=MISSING_EVIDENCE_PATH
        ),
    )


def fixture_receipt_url_wrong_host(paths):
    return _fault_at_board(
        paths, "0.4c-demoted.json", _mutated(paths, "0.4c-demoted.json", runUrl=RECEIPT_RUN_URL_WRONG_HOST)
    )


def fixture_receipt_frozen_count_string(paths):
    return _fault_at_board(
        paths, "0.5-factory-frozen.json", _mutated(paths, "0.5-factory-frozen.json", frozenCount="12")
    )


def fixture_receipt_frozen_count_bool(paths):
    """`True == 1` in Python, so a naive `isinstance(v, int)` takes this one."""
    return _fault_at_board(
        paths, "0.5-factory-frozen.json", _mutated(paths, "0.5-factory-frozen.json", frozenCount=True)
    )


def fixture_receipt_post_contexts_four(paths):
    """Four of the canonical five, `Batch Compile` KEPT -- so the row tests the COUNT alone."""
    return _fault_at_board(
        paths,
        "0.4b-required-checks.json",
        _mutated(
            paths,
            "0.4b-required-checks.json",
            postContexts=list(CANONICAL_04B_CONTEXTS)[1:],
        ),
    )


def fixture_control_forged_name(paths):
    """The hub's reproduction: a receipt outside the chain, otherwise entirely plausible.

    It carries a conforming stamp LATER than `0.7`, a well-formed `hashes` and the real
    provenance, so under the old `execution-control-*.json` GLOB it would have been SELECTED
    as the newest.  The only thing wrong with it is its NAME.
    """
    _kill_switch_receipts(
        paths,
        extra=(
            (
                _control_name(FORGED_CONTROL_STEP),
                _execution_control(paths, FORGED_CONTROL_STEP, CONTROL_STAMP_LATER),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_without_hashes(paths):
    _kill_switch_receipts(
        paths,
        extra=(
            (
                _control_name("0.7"),
                _execution_control(paths, "0.7", CONTROL_STAMP_07, drop=("hashes",)),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_parity_mismatch(paths):
    """A 0.35 whose `roadmapParityReceiptSha256` is a well-formed digest OF NOTHING.

    Exactly the forged set's provenance: right shape, bound to no file on this board.  0.35
    sorts first among the three chain names, so the refusal names it.
    """
    _kill_switch_receipts(
        paths,
        stamps={"0.35": RECEIPT_STAMP, "0.6": CONTROL_STAMP_06, "0.7": CONTROL_STAMP_07},
        extra=(
            (
                _control_name("0.35"),
                _execution_control(paths, "0.35", RECEIPT_STAMP, parity="f" * 64),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_product_live_14(paths):
    _kill_switch_receipts(
        paths,
        extra=(
            (
                _control_name("0.7"),
                _execution_control(paths, "0.7", CONTROL_STAMP_07, product_live_count=14),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_06_newer_than_07(paths):
    """A CHAIN VIOLATION: both valid, but the parsed-newest is 0.6 and the SELECTED one is 0.7."""
    _kill_switch_receipts_stamped(paths, CONTROL_STAMP_LATER, CONTROL_STAMP_WHOLE)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_o152_repair_06_between_04bi_and_07(paths):
    """O177: an O152 repair of 0.6, order-preserving.

    Three valid chain receipts -- 0.4b-i, 0.6 and 0.7 -- with 0.6's stamp sitting strictly
    between 0.4b-i's parsed stamp and a valid 0.7's.  0.7 remains both the parsed-newest and
    the SELECTED receipt, so this is the ALLOW half of O177's pair.
    """
    _kill_switch_receipts(
        paths,
        stamps={
            "0.4b-i": CONTROL_STAMP_O177_04B_I,
            "0.6": CONTROL_STAMP_O177_06_REPAIRED_BETWEEN,
            "0.7": CONTROL_STAMP_O177_07,
        },
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_o152_repair_06_at_write_time(paths):
    """O177's DENY partner: the SAME repair, stamped at the MOMENT OF THE WRITE.

    Identical to `fixture_control_o152_repair_06_between_04bi_and_07` except 0.6's stamp,
    which now sorts LATER than 0.7's -- exactly the undecidable state an O152 repair must
    never carry, and the moment-of-write stamp O177 forbids.
    """
    _kill_switch_receipts(
        paths,
        stamps={
            "0.4b-i": CONTROL_STAMP_O177_04B_I,
            "0.6": CONTROL_STAMP_O177_06_REPAIRED_AT_WRITE,
            "0.7": CONTROL_STAMP_O177_07,
        },
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_07_invalid_beside_valid_06(paths):
    """O152, STRICT.  0.7 is PRESENT and invalid (no `queueSha256`); 0.6 is valid.

    This is the fixture the "exclude" reading fails on.  Exclude the invalid 0.7 and the
    selection falls back to a valid 0.6, the literal below names 0.6 with 0.6's real digest,
    and the enable is ALLOWED against a STALE receipt -- O141's hazard in a new shape.  The
    strict rule refuses instead, naming 0.7.
    """
    _kill_switch_receipts(
        paths,
        extra=(
            (
                _control_name("0.7"),
                _execution_control(paths, "0.7", CONTROL_STAMP_07, drop=("queueSha256",)),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_selected_absent(paths):
    """A valid chain that stops at 0.35: neither 0.7 nor 0.6 exists, so the SELECTED one is absent."""
    _kill_switch_receipts(
        paths, stamps={"0.1": CONTROL_STAMP_WHOLE, "0.35": CONTROL_STAMP_LATER}
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_chain_035_and_06_at_board(paths):
    """The ALLOW with 0.7 ABSENT: the selection falls back to 0.6, which is also the newest."""
    _kill_switch_receipts(
        paths, stamps={"0.35": CONTROL_STAMP_WHOLE, "0.6": CONTROL_STAMP_LATER}
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_chain_all_six_names_at_board(paths):
    """The ALLOW with the WHOLE chain present and valid, ascending, 0.7 newest and selected."""
    _kill_switch_receipts(paths, stamps=dict((step, _chain_stamp(step)) for step in CHAIN_STEPS))
    return {VENUE_KEY: paths["BOARD"]}


# ------------------------------------------- S120: the second key's approval, the fixed set
#
# Each S120 fixture differs from `fixture_receipts_all_six_at_board` in EXACTLY one keyword of
# one chain receipt, at the board venue, so each DENY is attributable to the arm it names.


def _control_at_board(paths, step, stamp, **changes):
    """The default chain with ONE receipt replaced by a one-keyword variant, at the board venue."""
    _kill_switch_receipts(
        paths, extra=((_control_name(step), _execution_control(paths, step, stamp, **changes)),)
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_without_reviewed_head(paths):
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, drop=("reviewedHeadSha",))


def fixture_control_reviewed_head_uppercase(paths):
    """The RIGHT head, uppercased -- a git object name is lowercase, and the comparison is exact."""
    return _control_at_board(
        paths, "0.7", CONTROL_STAMP_07, reviewed_head=_head_sha("0.7").upper()
    )


def fixture_control_without_verdict_path(paths):
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, drop=("solVerdictPath",))


def fixture_control_verdict_changes_requested(paths):
    """A real verdict file at 0.7's head whose terminal block says CHANGES_REQUESTED."""
    return _control_at_board(
        paths,
        "0.7",
        CONTROL_STAMP_07,
        verdict_path=_verdict_relative("0.7", VERDICT_CHANGES_REQUESTED),
    )


def fixture_control_verdict_of_another_head(paths):
    """0.7 names 0.6's verdict: a real APPROVE, of a DIFFERENT head."""
    return _control_at_board(
        paths, "0.7", CONTROL_STAMP_07, verdict_path=_verdict_relative("0.6")
    )


def fixture_control_hashes_missing_key(paths):
    """0.6 -- NOT the selected receipt -- with one BASE path dropped from its `hashes`."""
    hashes = _board_hashes(paths, "0.6")
    del hashes[HASHES_DROPPED_PATH]
    return _control_at_board(paths, "0.6", CONTROL_STAMP_06, hashes=hashes)


def fixture_control_hashes_extra_key(paths):
    """0.7 with a path no step landed added to its `hashes`, at a well-formed digest."""
    hashes = _board_hashes(paths, "0.7")
    hashes[HASHES_EXTRA_PATH] = _derived_sha256("extra:" + HASHES_EXTRA_PATH)
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, hashes=hashes)


def fixture_control_01_without_composer_status(paths):
    """A chain of {0.1, 0.7} whose 0.1 lacks `composerStatus` and is otherwise complete."""
    _kill_switch_receipts(
        paths,
        stamps={"0.1": _chain_stamp("0.1"), "0.7": _chain_stamp("0.7")},
        extra=(
            (
                _control_name("0.1"),
                _execution_control(
                    paths, "0.1", _chain_stamp("0.1"), drop=("composerStatus",)
                ),
            ),
        ),
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_product_live_string(paths):
    """The right digits and the wrong type: `"15"` is not 15."""
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, product_live_count="15")


# ------------------------------------------- S123: the second sha, the merge commit
#
# Each S123 fixture differs from `fixture_receipts_all_six_at_board` in EXACTLY the `mergeSha`
# of one chain receipt, at the board venue.  The verdict every receipt names is untouched --
# sol's APPROVE of the REVIEWED head -- so the ALLOW rows prove the verdict is bound to
# `reviewedHeadSha` and the DENY rows are attributable to the merge sha's shape alone.


def fixture_control_without_merge_sha(paths):
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, drop=("mergeSha",))


def fixture_control_06_without_merge_sha(paths):
    """0.6 -- NOT the selected receipt -- without its merge sha: O152's reach, measured again."""
    return _control_at_board(paths, "0.6", CONTROL_STAMP_06, drop=("mergeSha",))


def fixture_control_merge_sha_uppercase(paths):
    """The RIGHT merge sha, uppercased -- a git object name is lowercase, compared exactly."""
    return _control_at_board(
        paths, "0.7", CONTROL_STAMP_07, merge_sha=_merge_sha("0.7").upper()
    )


def fixture_control_06_merge_sha_39_chars(paths):
    """The right merge sha cut to 39 characters, on 0.6: a prefix of a sha is not a sha."""
    return _control_at_board(paths, "0.6", CONTROL_STAMP_06, merge_sha=_merge_sha("0.6")[:39])


# The EXPLICIT merge sha of the normal-shape ALLOW row: derived from its own label, so it is
# neither the default `_merge_sha("0.7")` nor the reviewed head, and the structural test
# below asserts both.
MERGE_SHA_EXPLICIT_LABEL = "merge:0.7:the-github-merge-commit"


def fixture_control_merge_sha_differs_from_reviewed_head(paths):
    """The normal post-merge shape: an EXPLICIT merge sha that is not the reviewed head, ALLOW.

    The verdict the receipt names is sol's APPROVE of `_head_sha("0.7")`, the REVIEWED head;
    the merge sha is a different derived commit.  The row is ALLOW only while the hook binds
    the verdict to `reviewedHeadSha` and judges `mergeSha` for shape alone.
    """
    return _control_at_board(
        paths, "0.7", CONTROL_STAMP_07, merge_sha=_derived_sha(MERGE_SHA_EXPLICIT_LABEL)
    )


def fixture_control_merge_sha_equals_reviewed_head(paths):
    """A rebase lands the reviewed commit itself as the tip: the two shas are EQUAL, ALLOW."""
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, merge_sha=_head_sha("0.7"))


# ------------------------------------------- O172: the fixed-set equality proof
#
# Each O172 fixture differs from `fixture_receipts_all_six_at_board` in EXACTLY the
# `fixedSetEqualityProof` of 0.7, at the board venue; every other receipt carries the proof
# DERIVED from its own shas and hashes.  So each DENY is attributable to the one part it
# varies, and the ALLOW rows prove the derived proof is what the hook recomputes.


def fixture_control_without_equality_proof(paths):
    return _control_at_board(paths, "0.7", CONTROL_STAMP_07, drop=("fixedSetEqualityProof",))


def fixture_control_equality_proof_malformed(paths):
    """The RIGHT proof with its digest UPPERCASED -- `Get-FileHash`'s case (O145), on the proof."""
    hashes = _board_hashes(paths, "0.7")
    derived = _equality_proof(_head_sha("0.7"), _merge_sha("0.7"), hashes)
    shas, digest = derived.rsplit(":", 1)
    return _control_at_board(
        paths,
        "0.7",
        CONTROL_STAMP_07,
        hashes=hashes,
        equality_proof=shas + ":" + digest.upper(),
    )


def fixture_control_equality_proof_first_sha_mismatch(paths):
    """A well-formed proof whose FIRST sha is 0.6's reviewed head: the record of some other review."""
    hashes = _board_hashes(paths, "0.7")
    return _control_at_board(
        paths,
        "0.7",
        CONTROL_STAMP_07,
        hashes=hashes,
        equality_proof=_equality_proof(_head_sha("0.6"), _merge_sha("0.7"), hashes),
    )


def fixture_control_equality_proof_second_sha_mismatch(paths):
    """A well-formed proof whose SECOND sha is 0.6's merge commit."""
    hashes = _board_hashes(paths, "0.7")
    return _control_at_board(
        paths,
        "0.7",
        CONTROL_STAMP_07,
        hashes=hashes,
        equality_proof=_equality_proof(_head_sha("0.7"), _merge_sha("0.6"), hashes),
    )


def fixture_control_equality_proof_digest_mismatch(paths):
    """0.7's own two shas with the digest of 0.35's fixed set -- a proof PASTED from another receipt.

    0.35's set is the one with a DIFFERENT key set: 0.6 and 0.7 share one, and every file
    under the tmp board hashes the same on both, so 0.6's digest would be 0.7's own.  The
    structural test asserts the two digests really differ.
    """
    return _control_at_board(
        paths,
        "0.7",
        CONTROL_STAMP_07,
        equality_proof=_equality_proof(
            _head_sha("0.7"), _merge_sha("0.7"), _board_hashes(paths, "0.35")
        ),
    )


# ------------------------------------------- O158: the BOARD drifts from the selected receipt
#
# Neither fixture touches a receipt.  Both write the default conforming set, then mutate the
# tmp board's tooling AFTER the receipts recorded it -- which is precisely the state 0.2's
# re-hash exists to catch, and which no receipt-only validation can see.


def fixture_control_rehash_drift_at_board(paths):
    _kill_switch_receipts(paths)
    _write(
        _hashed_file_path(paths, REHASH_DRIFTED_PATH),
        "fixed-set fixture: %s -- edited after the receipt was written\n" % REHASH_DRIFTED_PATH,
    )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_control_rehash_file_missing_at_board(paths):
    _kill_switch_receipts(paths)
    os.remove(_hashed_file_path(paths, REHASH_MISSING_PATH))
    return {VENUE_KEY: paths["BOARD"]}


# ------------------------------------------- S120: one COMPLETE receipt per chain step
#
# Six ALLOW fixtures, one per step, each carrying that step's EXACT fixed set and its own
# keys.  The four early steps sit beside a 0.7 so a selected receipt exists; 0.6 and 0.7
# stand alone, so each is ALSO the selected receipt whose fixed set O158 re-hashes.


def _chain_step_complete_at_board(paths, step):
    stamps = {step: _chain_stamp(step)}
    if step not in ("0.6", "0.7"):
        stamps["0.7"] = _chain_stamp("0.7")
    _kill_switch_receipts(paths, stamps=stamps)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_chain_step_01_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.1")


def fixture_chain_step_035_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.35")


def fixture_chain_step_04c_i_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.4c-i")


def fixture_chain_step_04b_i_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.4b-i")


def fixture_chain_step_06_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.6")


def fixture_chain_step_07_complete_at_board(paths):
    return _chain_step_complete_at_board(paths, "0.7")


# ------------------------------------------- O159: the 0.18 repair and its re-pointing
#
# The hub procedure, modelled: the whole chain is written and valid, then
# `0.18-roadmap-parity.json` is REWRITTEN -- non-shrinking, conforming, longer by one key --
# and every chain receipt from 0.35 on is rewritten to carry the repaired file's digest with
# `queueSha256` and `productLiveCount` exactly as they were.  `left_behind` names the steps
# the partial variant does NOT re-point.


# O171: the stamp a hub that stamped "at the moment it writes" would put on EVERY receipt the
# cascade rewrites -- one instant, conforming, later than every original.
O159_RESTAMP = "2026-09-06T17:00:00Z"


def _o159_repaired_chain(paths, left_behind=(), restamped=()):
    """The cascade.  `left_behind` names steps NOT re-pointed; `restamped` names steps whose
    rewrite carries `O159_RESTAMP` instead of the ORIGINAL stamp (O171) -- the default,
    ``()``, is the register's procedure: every rewritten receipt keeps its own."""
    stamps = dict((step, _chain_stamp(step)) for step in CHAIN_STEPS)
    _kill_switch_receipts(paths, stamps=stamps)
    parity_path = os.path.join(paths["RECEIPTS"], ROADMAP_PARITY_RECEIPT)
    with open(parity_path, "rb") as handle:
        before = handle.read()
    repaired = _receipt_payloads(paths)[ROADMAP_PARITY_RECEIPT]
    repaired["repair"] = "O152 non-shrinking rewrite, ratification round 23"
    text = json.dumps(repaired, indent=2)
    if len(text.encode("utf-8")) < len(before):
        raise AssertionError("the O159 fixture must model a NON-shrinking rewrite")
    _write(parity_path, text)
    repaired_sha = _sha256_file(parity_path)
    for step in CHAIN_STEPS:
        if step in CHAIN_PROVENANCE_EXEMPT or step in left_behind:
            continue
        stamp = O159_RESTAMP if step in restamped else stamps[step]
        _write(
            os.path.join(paths["RECEIPTS"], _control_name(step)),
            _execution_control(paths, step, stamp, parity=repaired_sha),
        )
    return {VENUE_KEY: paths["BOARD"]}


def fixture_o159_repaired_chain_at_board(paths):
    return _o159_repaired_chain(paths)


def fixture_o159_partial_repair_at_board(paths):
    return _o159_repaired_chain(paths, left_behind=("0.35",))


def fixture_o159_cascade_restamped_to_a_tie_at_board(paths):
    """O171: the complete cascade, but the rewrite RESTAMPED 0.6 and 0.7 at its own instant.

    A hub that stamped each rewritten receipt "at the moment it writes" -- the rule for every
    OTHER receipt -- lands the two it rewrites last in the same second: two chain receipts,
    one stamp, no newest.  The register's ONE exception to that rule is exactly this
    cascade, and this fixture is what the exception costs when it is not honoured.
    """
    return _o159_repaired_chain(paths, restamped=("0.6", "0.7"))


def fixture_receipts_04b_ready(paths):
    _write(paths["SNAPSHOT"], _snapshot_rows(paths))
    _write(
        os.path.join(paths["RECEIPTS"], "0.4a-batch-compile-falsifier.json"),
        json.dumps({"failingContext": "Batch Compile", "runId": 1}),
    )
    _write(
        os.path.join(paths["RECEIPTS"], "0.4c-guardrail-move.json"),
        json.dumps({"conclusion": "success", "runId": 2}),
    )
    return {}


def fixture_receipts_04b_already_done(paths):
    fixture_receipts_04b_ready(paths)
    _write(
        os.path.join(paths["RECEIPTS"], "0.4b-required-checks.json"),
        json.dumps({"step": "0.4b", "contexts": list(CANONICAL_04B_CONTEXTS)}),
    )
    return {}


def _manifest_files(paths):
    """Write the four ratified surfaces, plus the `queue.json` the venue control needs."""
    for template in MANIFEST_SURFACES:
        _write(template.replace("{DUAL}", paths["DUAL"]), MANIFEST_EXISTING)
    _write(os.path.join(paths["DUAL"], "queue.json"), MANIFEST_EXISTING)


def fixture_manifest_at_board(paths):
    """O124/O126: `--project-dir` IS the board root -- exception (iv) is open."""
    _manifest_files(paths)
    return {VENUE_KEY: paths["BOARD"]}


def fixture_manifest_at_worktree(paths):
    """The same surfaces, a lane's worktree venue -- the exception does not apply."""
    _manifest_files(paths)
    return {VENUE_KEY: paths["WORKTREE"]}


def fixture_manifest_venue_absent(paths):
    """No venue ARGUMENT at all.  ``{}`` really means absent: `_invoke` passes none."""
    _manifest_files(paths)
    return {}


def fixture_manifest_venue_env_only(paths):
    """O126's regression falsifier: the venue in the ENVIRONMENT and NOT on the command line.

    The rev-19 hook read exactly this variable, and real hook processes were measured never
    to carry it.  A hook that reads it again turns this row ALLOW and goes red here.
    """
    _manifest_files(paths)
    return {"CLAUDE_PROJECT_DIR": paths["BOARD"]}


def fixture_venue_at_board(paths):
    """NA-10: hook evolution is authored at the board venue, and only there."""
    return {VENUE_KEY: paths["BOARD"]}


def fixture_venue_at_worktree(paths):
    """NA-10: a lane, rooted at the worktree whose gate it is being judged against."""
    return {VENUE_KEY: paths["REPO"]}


def fixture_checkpoint_archived(paths):
    target = os.path.join(paths["DUAL"], "orchestrator-resume-CURRENT.md")
    _write(target, CHECKPOINT_LONG)
    _write(
        os.path.join(paths["BOARD"], ".claude-state", "continuity", "archive", "orchestrator-20260906.md"),
        CHECKPOINT_LONG,
    )
    return {}


def fixture_checkpoint_unarchived(paths):
    _write(os.path.join(paths["DUAL"], "orchestrator-resume-CURRENT.md"), CHECKPOINT_LONG)
    return {}


FIXTURES = {
    "default": fixture_default,
    "snapshot": fixture_snapshot,
    "snapshot_malformed_tail": fixture_snapshot_malformed_tail,
    "snapshot_absent": fixture_snapshot_absent,
    "clip_authorized": fixture_clip_authorized,
    "existing_receipt": fixture_existing_receipt,
    # HOOK-FALSE-POSITIVE-1
    "existing_ledger": fixture_existing_ledger,
    # The UNSET half of the expansion pairs, and the unresolvable-reference rows.
    "lane_prompt_unset": fixture_lane_prompt_unset,
    "board_root_unset": fixture_board_root_unset,
    "receipts_all_six": fixture_receipts_all_six,
    "receipts_all_six_at_board": fixture_receipts_all_six_at_board,
    "receipts_all_six_at_worktree": fixture_receipts_all_six_at_worktree,
    "receipts_all_six_plus_enable": fixture_receipts_all_six_plus_enable,
    # S125
    "preflight_artifact_present_at_board": fixture_preflight_artifact_present_at_board,
    "receipts_missing_one": fixture_receipts_missing_one,
    "receipts_no_recorded_utc": fixture_receipts_no_recorded_utc,
    "control_stamp_fraction_at_board": fixture_control_stamp_fraction_at_board,
    "control_stamp_offset_at_board": fixture_control_stamp_offset_at_board,
    "control_stamp_naive_at_board": fixture_control_stamp_naive_at_board,
    "control_stamp_lowercase_z_at_board": fixture_control_stamp_lowercase_z_at_board,
    "control_stamps_tied_at_board": fixture_control_stamps_tied_at_board,
    "control_stamps_ordered_at_board": fixture_control_stamps_ordered_at_board,
    # S118 / O152
    "receipt_018_empty": fixture_receipt_018_empty,
    "receipt_04b_empty": fixture_receipt_04b_empty,
    "receipt_04c_empty": fixture_receipt_04c_empty,
    "receipt_06_empty": fixture_receipt_06_empty,
    "receipt_05_empty": fixture_receipt_05_empty,
    "receipt_018_missing_key": fixture_receipt_018_missing_key,
    "receipt_04b_missing_key": fixture_receipt_04b_missing_key,
    "receipt_04c_missing_key": fixture_receipt_04c_missing_key,
    "receipt_06_missing_key": fixture_receipt_06_missing_key,
    "receipt_05_missing_key": fixture_receipt_05_missing_key,
    "receipt_sha256_uppercase": fixture_receipt_sha256_uppercase,
    "receipt_sha256_63_chars": fixture_receipt_sha256_63_chars,
    "receipt_path_missing": fixture_receipt_path_missing,
    "receipt_url_wrong_host": fixture_receipt_url_wrong_host,
    "receipt_frozen_count_string": fixture_receipt_frozen_count_string,
    "receipt_frozen_count_bool": fixture_receipt_frozen_count_bool,
    "receipt_post_contexts_four": fixture_receipt_post_contexts_four,
    "control_forged_name": fixture_control_forged_name,
    "control_without_hashes": fixture_control_without_hashes,
    "control_parity_mismatch": fixture_control_parity_mismatch,
    "control_product_live_14": fixture_control_product_live_14,
    "control_06_newer_than_07": fixture_control_06_newer_than_07,
    # O177
    "control_o152_repair_06_between_04bi_and_07": fixture_control_o152_repair_06_between_04bi_and_07,
    "control_o152_repair_06_at_write_time": fixture_control_o152_repair_06_at_write_time,
    "control_07_invalid_beside_valid_06": fixture_control_07_invalid_beside_valid_06,
    "control_selected_absent": fixture_control_selected_absent,
    "chain_035_and_06_at_board": fixture_chain_035_and_06_at_board,
    "chain_all_six_names_at_board": fixture_chain_all_six_names_at_board,
    # S120 / O158 / O159
    "control_without_reviewed_head": fixture_control_without_reviewed_head,
    "control_reviewed_head_uppercase": fixture_control_reviewed_head_uppercase,
    "control_without_verdict_path": fixture_control_without_verdict_path,
    "control_verdict_changes_requested": fixture_control_verdict_changes_requested,
    "control_verdict_of_another_head": fixture_control_verdict_of_another_head,
    "control_hashes_missing_key": fixture_control_hashes_missing_key,
    "control_hashes_extra_key": fixture_control_hashes_extra_key,
    "control_01_without_composer_status": fixture_control_01_without_composer_status,
    "control_product_live_string": fixture_control_product_live_string,
    # S123
    "control_without_merge_sha": fixture_control_without_merge_sha,
    "control_06_without_merge_sha": fixture_control_06_without_merge_sha,
    "control_merge_sha_uppercase": fixture_control_merge_sha_uppercase,
    "control_06_merge_sha_39_chars": fixture_control_06_merge_sha_39_chars,
    "control_merge_sha_differs_from_reviewed_head": fixture_control_merge_sha_differs_from_reviewed_head,
    "control_merge_sha_equals_reviewed_head": fixture_control_merge_sha_equals_reviewed_head,
    # O172
    "control_without_equality_proof": fixture_control_without_equality_proof,
    "control_equality_proof_malformed": fixture_control_equality_proof_malformed,
    "control_equality_proof_first_sha_mismatch": fixture_control_equality_proof_first_sha_mismatch,
    "control_equality_proof_second_sha_mismatch": fixture_control_equality_proof_second_sha_mismatch,
    "control_equality_proof_digest_mismatch": fixture_control_equality_proof_digest_mismatch,
    "control_rehash_drift_at_board": fixture_control_rehash_drift_at_board,
    "control_rehash_file_missing_at_board": fixture_control_rehash_file_missing_at_board,
    "chain_step_01_complete_at_board": fixture_chain_step_01_complete_at_board,
    "chain_step_035_complete_at_board": fixture_chain_step_035_complete_at_board,
    "chain_step_04c_i_complete_at_board": fixture_chain_step_04c_i_complete_at_board,
    "chain_step_04b_i_complete_at_board": fixture_chain_step_04b_i_complete_at_board,
    "chain_step_06_complete_at_board": fixture_chain_step_06_complete_at_board,
    "chain_step_07_complete_at_board": fixture_chain_step_07_complete_at_board,
    "o159_repaired_chain_at_board": fixture_o159_repaired_chain_at_board,
    "o159_partial_repair_at_board": fixture_o159_partial_repair_at_board,
    # O171
    "o159_cascade_restamped_to_a_tie_at_board": fixture_o159_cascade_restamped_to_a_tie_at_board,
    "receipts_04b_ready": fixture_receipts_04b_ready,
    "receipts_04b_already_done": fixture_receipts_04b_already_done,
    "checkpoint_archived": fixture_checkpoint_archived,
    "checkpoint_unarchived": fixture_checkpoint_unarchived,
    "manifest_at_board": fixture_manifest_at_board,
    "manifest_at_worktree": fixture_manifest_at_worktree,
    "manifest_venue_absent": fixture_manifest_venue_absent,
    "manifest_venue_env_only": fixture_manifest_venue_env_only,
    "venue_at_board": fixture_venue_at_board,
    "venue_at_worktree": fixture_venue_at_worktree,
}


# ------------------------------------------------------------------------- runner


class MlvNeverAuthorizedHookTests(unittest.TestCase):
    """One method per table row.  Every path is a tmp fixture; nothing is executed."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlv-na-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        board = os.path.join(self.tmp, "board")
        dual = os.path.join(board, ".claude-state", "coordination", "dual-lane")
        self.paths = {
            "BOARD": board,
            "DUAL": dual,
            "RECEIPTS": os.path.join(dual, "receipts"),
            "SNAPSHOT": os.path.join(dual, "receipts", "required-checks-live.jsonl"),
            "OUTSIDE": os.path.join(self.tmp, "outside"),
            "CACHE": os.path.join(self.tmp, "cache"),
            # A lane's venue: a worktree root, never the board (O124/O126).
            "WORKTREE": os.path.join(self.tmp, "worktree"),
            # NA-10's targets live under the hook's OWN worktree root, because that is what
            # a lane editing its own gate actually looks like -- and because NA-7 allows
            # that tree by construction, so an NA-10 DENY there is attributable to NA-10.
            # It is derived, never a literal, so it is the checkout on either matrix leg.
            "REPO": REPO_ROOT,
            "CLIP_AUTH": os.path.join(board, "clips", "authorized", "take01.mlv"),
        }
        for key in ("BOARD", "DUAL", "RECEIPTS", "OUTSIDE", "CACHE", "WORKTREE"):
            os.makedirs(self.paths[key])
        # ROUND 3 (S134): the board root as the hook PRINTS it -- normalised, so a
        # `reason_contains` can pin the RESOLVED path rather than the placeholder.  That is
        # what turns the `$env:MLV_BOARD_ROOT` NA-2 row into a falsifier: a hook that does
        # not expand still DENIES that row, but names `$env:mlv_board_root/...` in the
        # reason, which this needle does not match.  Derived with the hook's OWN `norm`, so
        # it is right on both matrix legs and cannot drift from the printer.
        self.paths["BOARD_NORM"] = _load_hook_module().norm(self.paths["BOARD"])
        # ROUND 5: the hook's OWN built-in defaults, as the hook PRINTS them.  Read from the
        # module's constants and normalised with the module's `norm`, never written down
        # here: these needles must follow the hook if a default ever moves, and a literal
        # would turn a default change into a green suite plus a wrong claim.  They are the
        # expected values of the UNSET half of each pair -- what the hook resolves when the
        # steering variable is absent, which is the configuration that actually ships.
        self.paths["DEFAULT_BOARD_NORM"] = _load_hook_module().norm(
            _load_hook_module().DEFAULT_BOARD_ROOT
        )
        self.paths["CHECKPOINT_SHA"] = hashlib.sha256(
            CHECKPOINT_LONG.encode("utf-8")
        ).hexdigest()

    def _substitute(self, value):
        if isinstance(value, str):
            for key, replacement in self.paths.items():
                value = value.replace("{" + key + "}", replacement)
            return value
        if isinstance(value, dict):
            return dict((k, self._substitute(v)) for k, v in value.items())
        return value

    def _render_content(self, tool_input):
        """S125: render a `_preflight_content` spec to the JSON text the hub would write.

        AFTER substitution, because the compound inside carries tmp-board paths, and the
        backslashes of a Windows path are only valid inside a JSON string once
        `json.dumps` has escaped them -- which is the hub's artifact exactly: the stdin
        text, escapes included.
        """
        spec = tool_input.get("content")
        if not isinstance(spec, dict) or PREFLIGHT_CONTENT not in spec:
            return tool_input
        spec = spec[PREFLIGHT_CONTENT]
        text = json.dumps(
            {"tool_name": spec["tool_name"], "tool_input": {"command": spec["command"]}}
        )
        if spec["shape"] == "two":
            text = text + "\n" + text
        elif spec["shape"] == "not-json":
            text = text[:-1]
        elif spec["shape"] != "one":
            raise AssertionError("unknown pre-flight content shape %r" % spec["shape"])
        rendered = dict(tool_input)
        rendered["content"] = text
        return rendered

    def _chain_document(self, step):
        """The chain receipt for `step` as the fixture left it on the tmp board."""
        with open(os.path.join(self.paths["RECEIPTS"], _control_name(step)), "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))

    def _bind_the_enable_literal(self):
        """S112: resolve the enable literal's placeholders from the fixture ON DISK.

        The hook requires the literal to name the BASENAME of the newest valid
        execution-control receipt and that file's real lowercase sha256.  Both are read back
        out of the files the fixture just wrote -- never written down here -- so a hook that
        stopped opening the file, or opened a different one, goes RED.  The selection mirrors
        the hook's, and O141 pins it: newest by the PARSED value of the ONE pinned
        notation `YYYY-MM-DDTHH:MM:SSZ`, computed with `_pinned_moment`'s OWN parse rather
        than the hook's, so this table's idea of "newest" is never derived from the code it
        tests.  A receipt LACKING `recordedUtc`, or carrying a stamp in any OTHER notation,
        is skipped for this purpose only (the hook fails the whole exception closed on one,
        and every row that carries such a receipt denies at the receipt arm before any
        literal is read).

        `OLDER_CONTROL` is the OTHER valid receipt -- an older one that really exists and
        really validates, which is what makes the "not the newest" row a test of the
        SELECTION rather than of a typo.  `test_the_fixture_offers_an_older_valid_...`
        below asserts the fixture actually offers two, so that row can never quietly become
        a comparison of a name against itself.
        """
        controls = []
        try:
            names = sorted(os.listdir(self.paths["RECEIPTS"]))
        except OSError:
            names = []
        for name in names:
            if not (name.startswith("execution-control-") and name.endswith(".json")):
                continue
            with open(os.path.join(self.paths["RECEIPTS"], name), "rb") as handle:
                payload = handle.read()
            # S118/O152: EVERY chain receipt on disk also gets its own placeholder pair,
            # valid or not -- `{CONTROL_0_6}` and `{CONTROL_0_6_SHA}` for
            # `execution-control-0.6.json`, and so on.  The O152 row needs to name a
            # SPECIFIC receipt (the valid 0.6 beside a present-but-invalid 0.7), which
            # `{NEWEST_CONTROL}` cannot express: under the STRICT rule there is no newest at
            # all, and under the rejected "exclude" reading the newest would be the 0.6 --
            # so the row would silently change meaning with the rule it is testing.
            slug = re.sub(
                r"[^0-9A-Za-z]+", "_", name[len("execution-control-") : -len(".json")]
            ).upper()
            self.paths["CONTROL_" + slug] = name
            self.paths["CONTROL_" + slug + "_SHA"] = hashlib.sha256(payload).hexdigest()
            try:
                stamp = json.loads(payload.decode("utf-8")).get("recordedUtc")
            except ValueError:
                stamp = None
            moment = _pinned_moment(stamp)
            if moment is None:
                continue
            controls.append((moment, name, hashlib.sha256(payload).hexdigest()))
        controls.sort()
        if controls:
            newest = controls[-1]
            older = controls[0] if len(controls) > 1 else controls[-1]
        else:
            # No fixture wrote one.  Every row that reaches the literal arm DOES write them,
            # so these values only have to be something no receipt on disk can match.
            newest = (None, "execution-control-none.json", "0" * 64)
            older = newest
        self.paths.update(
            {
                "NEWEST_CONTROL": newest[1],
                "NEWEST_CONTROL_SHA": newest[2],
                "NEWEST_CONTROL_SHA_UPPER": newest[2].upper(),
                "NEWEST_CONTROL_SHA_SHORT": newest[2][:32],
                "OLDER_CONTROL": older[1],
                "OLDER_CONTROL_SHA": older[2],
            }
        )

    def _invoke(self, stdin_text, overrides):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("MLV_"):
                del env[key]
        # O126: the VENUE is an input, so it must be a PARAMETER of the row and never
        # something inherited -- and since the fifth delta it arrives as an ARGUMENT.  The
        # harness that runs this suite sets CLAUDE_PROJECT_DIR for its own project; it is
        # stripped here so that a hook which regressed to reading the environment cannot
        # look correct, and so that the rows asserting the ABSENT reading really are absent.
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["MLV_BOARD_ROOT"] = self.paths["BOARD"]
        env["MLV_CLIP_CACHE_ROOT"] = self.paths["CACHE"]
        env["MLV_REQUIRED_CHECKS_SNAPSHOT"] = self.paths["SNAPSHOT"]
        overrides = dict(overrides)
        venue = overrides.pop(VENUE_KEY, None)
        # ROUND 5: `UNSET` is not a value, it is the ABSENCE of one.  Popped before the
        # update (a sentinel object is not a legal environment value) and deleted from the
        # environment, so the hook resolves that name to its OWN built-in default -- the
        # configuration this machine actually runs in, and the one round 3 never measured.
        for key in [key for key, value in overrides.items() if value is UNSET]:
            overrides.pop(key)
            env.pop(key, None)
        env.update(overrides)
        env["PYTHONIOENCODING"] = "utf-8"
        argv = [sys.executable, HOOK]
        if venue is not None:
            argv += ["--project-dir", venue]
        completed = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=self.tmp,
        )
        return completed

    def _run_case(self, case):
        overrides = FIXTURES[case.get("fixture", "default")](self.paths)
        # AFTER the fixture, because S112's placeholders are derived from what it wrote.
        self._bind_the_enable_literal()
        if "raw" in case:
            stdin_text = case["raw"]
        else:
            stdin_text = json.dumps(
                {
                    "tool_name": case["tool"],
                    "tool_input": self._render_content(self._substitute(case["input"])),
                }
            )
        completed = self._invoke(stdin_text, overrides)
        detail = "\nrow: %s\nstdout: %r\nstderr: %r" % (
            case["name"],
            completed.stdout,
            completed.stderr,
        )
        if case["expect"] == "ALLOW":
            self.assertEqual(completed.returncode, 0, "expected ALLOW" + detail)
            self.assertEqual(completed.stderr.strip(), "", "ALLOW must be silent" + detail)
            return
        self.assertEqual(completed.returncode, 2, "expected DENY" + detail)
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "DENY must print exactly ONE line" + detail)
        self.assertTrue(
            lines[0].startswith(case["na"] + ":"),
            "expected the %s row to fire, got %r%s" % (case["na"], lines[0], detail),
        )
        # A register row can fire for the WRONG arm.  Where two rows differ in exactly one
        # variable and share a rule id, the row asserts the arm by its reason text too.
        if "reason_contains" in case:
            # Substituted like the input: an S112 row asserts the arm by quoting the very
            # value it varied, and that value is a fixture placeholder.  A row may give a
            # TUPLE of needles instead of one string, and the O141 rows do: the register
            # asks that the refusal name the FINDING and the OFFENDING FILE, which is two
            # claims about one line, and `assertIn` on a single long substring would make
            # them one brittle claim instead of two attributable ones.
            expected = case["reason_contains"]
            needles = (expected,) if isinstance(expected, str) else tuple(expected)
            self.assertTrue(needles, "reason_contains must not be empty" + detail)
            for needle in needles:
                needle = self._substitute(needle)
                self.assertIn(
                    needle,
                    lines[0],
                    "expected the reason to name %r%s" % (needle, detail),
                )

    # ------------------------------------------------------------- structural gates

    def test_hook_script_is_present_on_this_ref(self):
        """A hook's script must live on the SAME REF as the tree it guards."""
        self.assertTrue(os.path.isfile(HOOK), HOOK)

    def test_every_environment_input_the_hook_reads_is_na3_protected(self):
        """S133: THE RULE IS THE LIST, and the list is DERIVED, not remembered.

        An earlier revision of HOOK-FALSE-POSITIVE-1 shipped a steering input that WIDENS
        NA-7's roots while it was absent from `_NA3_PERSISTENT_NAMES` -- so `setx` of it was
        ALLOWED and every later hook process on the machine would have inherited a root of
        the reviewer's choosing.  A reviewer found it by trying it; nothing in this suite
        could have.  That root has since been removed from the gate, but the GATE THAT WOULD
        HAVE CAUGHT IT is kept, because it is about the next input, not that one.

        This gate reads the hook's OWN source for the environment names it consults and
        asserts each one is matched by the hook's OWN persistent-name regex, so the next
        input added to `Ctx.__init__` cannot ship unprotected.  `MLV_HOOK_DRYRUN` is the one
        exemption and it is named explicitly rather than pattern-matched: it selects whether
        the decision is PRINTED and cannot change any decision (`main` returns the same exit
        code either way), so a persistent write of it moves no ground.
        """
        with open(HOOK, "r", encoding="utf-8") as handle:
            source = handle.read()
        read_names = set(re.findall(r"env(?:iron)?\.get\(\s*[\"'](\w+)[\"']", source))
        self.assertIn("MLV_BOARD_ROOT", read_names, "the hook must read the board root")
        steering = sorted(read_names - {"MLV_HOOK_DRYRUN"})
        self.assertTrue(steering, "the hook reads no environment input at all?")
        name_rx = _load_hook_module()._NA3_PERSISTENT_NAME_RX
        unprotected = [name for name in steering if not name_rx.fullmatch(name)]
        self.assertEqual(
            unprotected,
            [],
            "environment inputs the hook READS but NA-3 does not protect: %s" % unprotected,
        )
        # And the table's own tuple is the same list, so a reader of either sees one answer.
        for name in steering:
            self.assertIn(name, PERSISTENT_NAMES, name)

    def test_every_environment_input_the_hook_reads_is_expanded_in_a_path(self):
        """S134: THE EXPANSION LIST IS THE READ LIST, DERIVED FROM THE SAME CALL SITES.

        An earlier revision shipped a guard whose docstring promised "denied at every value"
        while the code resolved only a LITERAL path; a reviewer broke it by spelling the same
        file through `$env:`.  The repair is only durable if the NEXT input added to
        `Ctx.__init__` is expanded too, so this gate derives the names from the hook's own
        `env.get(...)` call sites -- the SAME derivation the NA-3 protection gate above
        performs -- and asserts `HOOK_READ_ENV_NAMES` is EXACTLY that set.  Equality in both
        directions is the point: a missing name is that defect returning, and an EXTRA name
        is this hook substituting a value it never read, which is the fabrication the
        header's residual limit refuses.
        """
        with open(HOOK, "r", encoding="utf-8") as handle:
            source = handle.read()
        read_names = set(re.findall(r"env(?:iron)?\.get\(\s*[\"'](\w+)[\"']", source))
        module = _load_hook_module()
        self.assertEqual(
            sorted(module.HOOK_READ_ENV_NAMES),
            sorted(read_names),
            "the expansion list must be exactly the names the hook reads",
        )
        # And every spelling the list claims really resolves, at the value THIS process
        # carries -- one assertion per spelling, so a regex that loses one goes red by name.
        saved = os.environ.get("MLV_BOARD_ROOT")
        os.environ["MLV_BOARD_ROOT"] = r"C:\fp1\board"
        try:
            for spelling in (
                "$env:MLV_BOARD_ROOT/specs/x.md",
                "${env:MLV_BOARD_ROOT}/specs/x.md",
                "%MLV_BOARD_ROOT%/specs/x.md",
                "$ENV:mlv_board_root/specs/x.md",
            ):
                self.assertEqual(
                    module.norm(spelling), "c:/fp1/board/specs/x.md", spelling
                )
            # A name the hook does NOT read is left standing, not blanked and not guessed.
            self.assertEqual(
                module.norm("$env:FP1_UNREAD_ROOT/specs/x.md"),
                "$env:fp1_unread_root/specs/x.md",
            )
            # Expansion precedes the `..` collapse, which is the whole reason it lives in
            # `norm` rather than at a call site.
            self.assertEqual(
                module.norm("$env:MLV_BOARD_ROOT/../escaped.txt"), "c:/fp1/escaped.txt"
            )
            # ... and a reference that CANNOT be resolved is not collapsed at all.  It used
            # to become the bare relative `escaped.txt`, which NA-7's early return waved past
            # as "inside the worktree by construction"; `norm` now leaves the text standing
            # so `_na7_check_path` can refuse it as unresolvable.
            self.assertEqual(
                module.norm("$env:FP1_UNREAD_ROOT/../escaped.txt"),
                "$env:fp1_unread_root/../escaped.txt",
            )
            self.assertEqual(
                module._unresolved_env_ref("$env:fp1_unread_root/../escaped.txt"),
                "$env:fp1_unread_root",
            )
            # A path with NO reference in it is collapsed exactly as before -- the new rule
            # must not be doing the `..` collapse's job.
            self.assertIsNone(module._unresolved_env_ref("prompts/../escaped.txt"))
            self.assertEqual(module.norm("prompts/../escaped.txt"), "escaped.txt")
        finally:
            if saved is None:
                del os.environ["MLV_BOARD_ROOT"]
            else:
                os.environ["MLV_BOARD_ROOT"] = saved

    def test_every_register_row_has_at_least_one_deny_case(self):
        """The suite FAILS if any of NA-1,2,3,4,6,7,8,9,10 has zero DENY cases.

        NA-10 joined the list at register v18 (O129, hub extension): a rule the hook
        enforces but the table does not falsify is a rule nobody can tell is still wired.
        """
        covered = set(
            case["na"] for case in CASES if case["expect"] == "DENY" and "na" in case
        )
        missing = [row for row in REGISTER_ROWS_WITH_DENY_CASES if row not in covered]
        self.assertEqual(missing, [], "register rows with zero DENY cases: %s" % missing)

    def test_table_counts_match_the_ratification_receipts(self):
        counts = {}
        for case in CASES:
            counts[case["group"]] = counts.get(case["group"], 0) + 1
        self.assertEqual(counts.get("control"), 3, "3 DENY controls")
        self.assertEqual(counts.get("round1"), 16, "16 round-1 falsifiers")
        self.assertEqual(counts.get("round2"), 12, "12 round-2 falsifiers")
        self.assertEqual(counts.get("failclosed"), 4, "4 fail-closed inputs")
        self.assertEqual(counts.get("benign"), 6, "6 benign ALLOW controls")
        # HOOK-FALSE-POSITIVE-1.  Pinned so a subject cannot be dropped once its control
        # still passes.  The count rose 15 -> 29 -> 39 -> 44 across five review rounds as the
        # pairs were re-cut and two claims (the `..` collapse, the environment expansion)
        # arrived with their own rows.
        #
        # PINNED DELIBERATELY AT THE NARROWING: 44 -> 30, AND THIS IS THE ONE TIME ROWS WERE
        # DROPPED ON PURPOSE.  Fourteen rows asserted behaviour of a THIRD NA-7 root -- a
        # sibling-repository write surface -- which is REMOVED from the gate: the four
        # reproducible bypasses five rounds turned up were all in that root's machinery, and
        # a row asserting a removed feature is not a falsifier, it is a resurrection notice.
        # What went with it: the bus publish/delete pair (v), the two bus `..` escapes (vii),
        # the two NA-3 `setx` rows for its name (viii), the six env/cmd/braced spelling rows
        # and their unread-name controls (ix), and four of the five UNSET twins (x).
        #
        # WHAT WAS KEPT, AND WHY IT IS NOT ENTANGLED WITH WHAT WENT.  Every retained claim is
        # measured against a root that still exists: the `..` collapse is proved at the BOARD
        # root, the WORKTREE root and against an NA-2 protected tail (vii); the environment
        # expansion is proved on `$env:MLV_BOARD_ROOT` in BOTH the bound and unbound
        # configurations, where it closed a real ALLOW on the protected queue ledger
        # (viii/ix); the bit-bucket sinks, the redirect-target precision and the
        # create-under-coordination arm never named a root at all (i, ii, iii, iv, vi).
        #
        # THREE ROWS ARE NEW.  (v) pins the NARROWING itself as a DENY, so the removal is a
        # rule a reader can see rather than an absence they might read as an oversight; (x)
        # is the round-5 blocker, which SURVIVES the narrowing because it was never about the
        # removed root -- a reference the hook cannot resolve used to collapse into a bare
        # relative name that NA-7's early return waved through -- with two controls beside it
        # so the new refusal is attributable and is not doing the `..` collapse's job.
        self.assertEqual(
            counts.get("falsepositive1"), 30, "30 HOOK-FALSE-POSITIVE-1 rows"
        )
        # PINNED DELIBERATELY, 0.05 third review delta: the 0.2 enable became ONE dedicated
        # act, so the group grew 6 -> 15.  Six rows vary the canonical act's PRECONDITIONS,
        # six vary its SHAPE (all DENY), two are exception (iii) creates and one is the
        # bare receipt write.  The count is here so a row cannot be dropped silently; the
        # round-1 (16) and round-2 (12) falsifier counts above are historical and unchanged.
        #
        # PINNED DELIBERATELY, 0.05 FOURTH review delta (S101): the canonical compound
        # became FAIL-CLOSED, so the group grew 15 -> 19.  The ALLOW row and the five rows
        # that vary its preconditions now carry the fail-closed shape (changed, not added);
        # the four NEW rows each drop or alter exactly one fail-closed token -- the rev-18
        # shape, `Remove-Item` without `-ErrorAction Stop`, a read-back naming a different
        # receipt, and a different `throw` literal.
        #
        # PINNED DELIBERATELY, 0.05 FIFTH review delta: 19 -> 23, four new rows and no row
        # dropped.  TWO are S105/O126 -- the enable act became VENUE-BOUND, so the canonical
        # compound at a worktree venue and with the venue argument absent are now DENY, and
        # the ALLOW row moved to the board venue (changed, not added).  TWO are O128 -- the
        # compound now OPENS with `$ErrorActionPreference = 'Stop'`, so the rev-19 shape and
        # a `Continue` preference are DENY.  Both pairs are one token from the ALLOW row.
        #
        # PINNED DELIBERATELY, 0.05 SIXTH review delta (S112): 23 -> 31, eight new rows and
        # no row dropped.  The enable LITERAL is now validated semantically instead of for
        # presence, and each new row varies exactly ONE of its fields against the same
        # fixture, same shape, same board venue: the execution-control receipt named is an
        # older valid one, then the right one written as a PATH; the digest is another
        # receipt's, then uppercase, then truncated; `enabledUtc` loses its `Z`,
        # `recordedUtc` carries an offset, then is not a timestamp at all.  The ALLOW row and
        # the NINETEEN other rows that already carried a literal were CHANGED, not added --
        # 28 of the 31 rows in this group now carry a literal naming the fixture's real
        # newest receipt and its real sha256 (computed at run time by
        # `_bind_the_enable_literal`, never written down).  The historical falsifier
        # groups -- `control` 3, `round1` 16, `round2` 12, `failclosed` 4, `benign` 6 -- are
        # untouched, as are `carveout`, `manifest`, `na3`, `na3_persistent` and `na10`.
        #
        # PINNED DELIBERATELY, 0.05 SEVENTH review delta (O141): 31 -> 40, nine new rows and
        # no row dropped.  ONE fixed notation now governs every `recordedUtc` the hook reads
        # or validates, and the newest execution-control receipt is selected by the PARSED
        # value.  FIVE rows vary a RECEIPT stamp against the same canonical compound, the
        # same six receipts and the same board venue -- the hub's own reproduction pair (a
        # fraction beside a whole second), an offset, a naive stamp, a lowercase `z`, and two
        # receipts stamped identically -- and each is DENY because the newest became
        # UNDECIDABLE, with the refusal naming the offending file, or both files for the tie.
        # TWO narrow the LITERAL to that same notation: `enabledUtc` and `recordedUtc` with
        # fractional seconds, which the sixth commit accepted.  TWO are the ALLOW/DENY pair
        # that pins WHICH receipt the selection returns -- `0.6` at `...16:00:00Z` beside
        # `0.7` at `...16:00:01Z`, the literal naming the newest (ALLOW) and naming the older
        # (DENY).  NO EXISTING ROW WAS RE-STAMPED: every fixture stamp already carried the
        # pinned notation, and `test_every_stamp_the_default_receipt_fixture_writes_...` now
        # locks that rather than leaving it true by accident.  The historical falsifier
        # groups -- `control` 3, `round1` 16, `round2` 12, `failclosed` 4, `benign` 6 -- are
        # untouched, as are `carveout`, `manifest`, `na3`, `na3_persistent` and `na10`.
        #
        # PINNED DELIBERATELY, 0.05 EIGHTH review delta (S118/O152): 40 -> 66, twenty-six
        # new rows and NO row dropped.  The six gate receipts now VALIDATE against the schema
        # table of plan 1.3 step 5 instead of merely parsing, which is the defect the hub
        # reproduced on the sixth commit -- five `{}` receipts beside
        # `execution-control-forged.json` carrying only the provenance keys ALLOWED the
        # enable.  TWENTY-FOUR are DENY: an EMPTY OBJECT in each of the five fixed receipts
        # (five rows, one per receipt, because "an empty object is INVALID" must be true of
        # every receipt and not of whichever is read first); a MISSING REQUIRED KEY in each
        # of the five, naming the key (five rows); the value classes, one row each -- an
        # uppercase sha256, a 63-character sha256, a `path` naming a file that does not
        # exist, a `url` on the UPSTREAM host, `frozenCount` as a string, `frozenCount` as a
        # BOOLEAN (`True == 1`, so the obvious `isinstance` takes it) and `postContexts` with
        # four entries; and the chain, one row each -- `execution-control-forged.json` beside
        # a valid chain (the candidate set is an ENUMERATION, not the glob it walked in
        # through), a chain receipt lacking `hashes`, a 0.35 whose
        # `roadmapParityReceiptSha256` is a well-formed digest of nothing, a
        # `productLiveCount` of 14, a valid 0.6 stamped LATER than a valid 0.7 (a chain
        # violation, undecidable), a PRESENT-but-invalid 0.7 beside a valid 0.6 with the
        # literal naming 0.6 (O152's strict rule -- the "exclude" reading ALLOWS this one),
        # and the SELECTED receipt absent.  TWO are ALLOW: a conforming chain of `{0.35,
        # 0.6}` with 0.7 absent, and all six chain names present, valid and ascending.
        #
        # NO ROW WAS RE-EXPECTED, but EVERY existing row's receipt fixture was REBUILT: the
        # placeholder payloads (`{"step": "0.18", "parity": true}`) are now fully conforming
        # documents whose every bound value -- the paths, the digests and the on-disk sha256
        # of `0.18-roadmap-parity.json` -- is COMPUTED at run time from the fixture, never
        # written down.  One fixture MOVED: `receipts_no_recorded_utc` used
        # `execution-control-0.5.json`, a name outside the chain, which the enumeration now
        # refuses BY NAME -- so that O105 row would have stopped testing O105.  It is `0.35`
        # now.  The historical falsifier groups -- `control` 3, `round1` 16, `round2` 12,
        # `failclosed` 4, `benign` 6 -- are untouched, as are `carveout`, `manifest`, `na3`,
        # `na3_persistent` and `na10`.
        #
        # PINNED DELIBERATELY, 0.05 NINTH review delta (S120/O158/O159): 66 -> 85, nineteen
        # new rows and NO row dropped.  A chain receipt now carries the second key's approval
        # of THIS head and an EXACT fixed hash set, and the enable act re-hashes that set in
        # the board root.  NINE are S120 DENY, one keyword of one receipt from the ALLOW
        # rows: `reviewedHeadSha` absent, then uppercased; `solVerdictPath` absent, then a
        # real verdict whose terminal block is CHANGES_REQUESTED, then a real APPROVE of a
        # DIFFERENT head; `hashes` lacking a BASE path (on the NON-selected 0.6, so O152's
        # reach is measured), then carrying a path no step landed; 0.1 lacking
        # `composerStatus`; and `productLiveCount` as the STRING "15" (the 14 row is the
        # wrong value, this is the wrong kind).  TWO are O158 DENY in which every receipt is
        # valid and the BOARD changed -- a file of the selected receipt's set edited after
        # the receipt recorded it, and one deleted -- refused naming the path.  SIX are
        # ALLOW, one per chain step, each carrying that step's exact set: with the
        # missing/extra-key rows they pin the hook's six sets to this suite's
        # `REQUIRED_HASHES`, two independent restatements of the plan's table.  TWO are
        # O159: the 0.18 receipt rewritten non-shrinking with every chain receipt from 0.35
        # on re-pointed (ALLOW), and the same with 0.35 left behind (DENY, naming it).
        #
        # NO ROW WAS RE-EXPECTED, and every existing chain fixture was REBUILT to carry the
        # new keys: `reviewedHeadSha` and a `solVerdictPath` the fixture WROTE (sol's
        # template shape, terminal ```json fence, an earlier BLOCKER fence above it), the
        # step's EXACT fixed set at the REAL digest of each file the fixture writes under the
        # tmp board's `tools/` tree, and 0.1/0.35's composer keys.  The historical falsifier
        # groups -- `control` 3, `round1` 16, `round2` 12, `failclosed` 4, `benign` 6 -- are
        # untouched, as are `carveout`, `manifest`, `na3`, `na3_persistent` and `na10`.
        #
        # PINNED DELIBERATELY, 0.05 TENTH review delta (S123): 85 -> 91, six new rows and NO
        # row dropped, no row re-expected.  A chain receipt now carries TWO shas: the
        # `reviewedHeadSha` the ninth commit already validated -- whose MEANING is pinned as
        # the PR head sol reviewed BEFORE the merge, the sha the verdict binds to, never the
        # merge commit -- and a second required `mergeSha`, the post-merge commit the hashes
        # were taken at.  FOUR are DENY, one keyword of one receipt from the ALLOW rows:
        # `mergeSha` absent on the selected 0.7 and on the NON-selected 0.6 (O152's reach),
        # then uppercased, then cut to 39 characters.  TWO are ALLOW: an EXPLICITLY different
        # merge sha beside a verdict of the reviewed head (the normal post-merge shape -- a
        # hook binding the verdict to the merge sha refuses it), and the two shas EQUAL (a
        # rebase; the hook judges shape only and never the merge method).  Every existing
        # chain fixture carries both keys, derived to differ.  The historical falsifier
        # groups -- `control` 3, `round1` 16, `round2` 12, `failclosed` 4, `benign` 6 -- are
        # untouched, as are `carveout`, `manifest`, `na3`, `na3_persistent` and `na10`.
        #
        # PINNED DELIBERATELY, 0.05 ELEVENTH review delta (S125/O172/O171): 91 -> 110,
        # nineteen new rows and NO row dropped, no row re-expected.  THIRTEEN are S125, the
        # PRE-FLIGHT ARTIFACT act -- a `Write` of exactly
        # `receipts/0.2-enable-preflight-input.json`, decided BEFORE generic content
        # attribution: ONE ALLOW (the valid stdin JSON of the canonical compound, at the
        # board venue, onto an absent target -- the input the hub measured ALLOWED by the
        # receipts carve-out on the tenth commit, now allowed BY RULE); TEN DENY one variable
        # from it (the same content to `notes.json` and under `fleet-runs/` -- content
        # attribution, which the tenth commit lacked for file tools; a worktree venue; an
        # existing target; content that is not JSON, two objects, an inner `tool_name` of
        # `Bash`; a command lacking the leading preference, naming a different receipt file,
        # and naming an OLDER execution-control receipt -- the semantic arm); ONE DENY for an
        # `Edit` of the artifact's path (the act is a `Write`); and ONE ALLOW control, an
        # ordinary receipt create beside the six, which keeps the carve-out a carve-out.
        # FIVE are O172, `fixedSetEqualityProof`, one part of 0.7's from the ALLOW rows:
        # absent, its digest uppercased (malformed), the FIRST sha another head, the SECOND
        # sha another merge, and a digest PASTED from 0.35's set.  ONE is O171: the O159
        # cascade restamped so 0.6 and 0.7 share the rewrite's instant (undecidable); its
        # ALLOW partner is the ninth commit's cascade row, whose fixture already preserved
        # every ORIGINAL stamp -- now asserted by
        # `test_the_o159_cascade_fixture_preserves_every_original_stamp_...`.  Every existing
        # chain fixture carries a proof DERIVED from its own shas and hashes.  The historical
        # falsifier groups -- `control` 3, `round1` 16, `round2` 12, `failclosed` 4, `benign`
        # 6 -- are untouched, as are `carveout`, `manifest`, `na3`, `na3_persistent` and
        # `na10`.
        #
        # PINNED DELIBERATELY, 0.05 TWELFTH review delta (O177): 110 -> 112, two new rows and
        # NO row dropped, no row re-expected.  Both are an O152 repair of `0.6` on a THREE-
        # receipt chain {0.4b-i, 0.6, 0.7}: one ALLOW with the repair's stamp strictly
        # BETWEEN 0.4b-i's parsed stamp and a valid 0.7's (order-preserving, the shape a
        # repair must take), and one DENY with the SAME repair stamped at the moment of the
        # write instead -- later than 0.7's, which makes the parsed-newest valid receipt 0.6
        # again, a CHAIN VIOLATION against the SELECTED 0.7.  O175's worked-digest and
        # pinned-command tests add no CASES row: they assert the hook's own canonicalisation
        # function directly, imported by `_load_hook_module`, and belong to no `group`. The
        # historical falsifier groups -- `control` 3, `round1` 16, `round2` 12, `failclosed`
        # 4, `benign` 6 -- are untouched, as are `carveout`, `manifest`, `na3`,
        # `na3_persistent` and `na10`.
        self.assertEqual(counts.get("killswitch"), 112, "112 kill-switch / 0.2-enable rows")
        # PINNED DELIBERATELY, 0.05 fourth review delta (O125): 3 -> 4.  The carve-out is a
        # PATH permission, not a TOOL permission, and the pair that proves it -- the `Write`
        # create ALLOW beside the shell `Set-Content` create DENY -- must not be separable.
        self.assertEqual(counts.get("carveout"), 4, "4 NA-2 carve-out rows")
        # PINNED DELIBERATELY, 0.05 fourth review delta (O124): a NEW group, 20 rows.  The
        # cross product of 4 ratified surfaces x 4 write shapes at the board venue (16
        # ALLOW), plus the worktree venue, the absent venue, the delete that stays denied at
        # every venue, and the `queue.json` control that proves the venue did not become a
        # blanket permission over `$D`.  Dropping any one of the last four would turn
        # exception (iv) from a gate into a wall or a hole without a red test.
        #
        # PINNED DELIBERATELY, 0.05 fifth review delta (O126): 20 -> 21.  The venue moved
        # from the environment to `--project-dir`, and the ONE new row is the regression
        # falsifier for that move: the variable set in the ENVIRONMENT, no argument, DENY.
        # Without it the rev-19 mechanism could come back and every other row would still
        # be green, which is exactly how it shipped unfired the first time.
        self.assertEqual(counts.get("manifest"), 21, "21 NA-2 exception (iv) venue rows")
        # PINNED, NEW at the 0.05 fifth review delta.  `na3` grew 4 -> 5 with the BARE
        # `claude auth status` ALLOW (S106) -- the card lists it under the benign controls,
        # but `benign` is a historical count and may not move, so it joins the auth group it
        # belongs to.  `na3_persistent` is O129: five DENY rows, one per steered name, both
        # persistent verbs covered, plus the in-process `$env:` ALLOW control that keeps the
        # rule about PERSISTENCE.  `na10` is the hub extension: two DENY writes at a lane
        # venue, the same two ALLOW at the board venue, the shell arm, and the unrelated-file
        # control that keeps it about the GATE and not about the worktree.  `na10` grew 6 -> 7
        # at NA4-OWNER-CONSENTED-FOOTAGE-1 round 3 (E): the consent verifier's DENY row.
        self.assertEqual(counts.get("na3"), 5, "5 NA-3 claude-auth rows")
        self.assertEqual(counts.get("na3_persistent"), 6, "6 NA-3 O129 persistent-scope rows")
        self.assertEqual(counts.get("na10"), 7, "7 NA-10 self-edit rows")
        # PINNED, NEW at the thirteenth commit (S131): a NEW group, 3 rows -- the `setx.exe`
        # credential-prefix bypass, the `setx.exe` persistent-name bypass, and the fully
        # qualified `[System.Environment]::SetEnvironmentVariable` persistent-name bypass.
        # `round2` (12) and `na3_persistent` (6) are historical and unchanged.
        self.assertEqual(counts.get("s131"), 3, "3 S131 .exe / fully-qualified bypass rows")

    def test_the_fixture_offers_an_older_valid_execution_control_receipt(self):
        """S112: the "not the newest" row must compare two DIFFERENT valid receipts.

        `_bind_the_enable_literal` falls back to the newest when a fixture writes only one
        execution-control receipt, and that fallback would quietly turn the selection row
        into a comparison of a name against itself -- ALLOW-shaped input, green row, no
        selection tested.  So the fixture's promise is asserted directly: two valid
        receipts, and the older one really is a file on disk that really validates.
        """
        fixture_receipts_all_six_at_board(self.paths)
        self._bind_the_enable_literal()
        newest = self.paths["NEWEST_CONTROL"]
        older = self.paths["OLDER_CONTROL"]
        self.assertNotEqual(newest, older, "the fixture must offer an OLDER receipt too")
        self.assertNotEqual(
            self.paths["NEWEST_CONTROL_SHA"], self.paths["OLDER_CONTROL_SHA"]
        )
        for name in (newest, older):
            path = os.path.join(self.paths["RECEIPTS"], name)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "rb") as handle:
                payload = handle.read()
            self.assertIsInstance(
                json.loads(payload.decode("utf-8")).get("recordedUtc"), str
            )
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                self.paths[
                    "NEWEST_CONTROL_SHA" if name == newest else "OLDER_CONTROL_SHA"
                ],
            )
        # The digest the hook must demand is LOWERCASE hex, and the two near-miss forms the
        # table varies are really near misses of it -- not accidentally equal to it.
        self.assertRegex(self.paths["NEWEST_CONTROL_SHA"], r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(
            self.paths["NEWEST_CONTROL_SHA"], self.paths["NEWEST_CONTROL_SHA_UPPER"]
        )
        self.assertEqual(len(self.paths["NEWEST_CONTROL_SHA_SHORT"]), 32)

    def test_the_o141_stamp_constants_are_what_they_claim(self):
        """O141: the near misses really are near misses, and the defect really inverts.

        Three claims, asserted rather than described.  (1) The two conforming stamps parse
        and order the way the ALLOW/DENY pair assumes.  (2) The four refused stamps are
        NON-EMPTY STRINGS that fail the pinned notation -- exactly what presence-only
        validation accepted.  (3) THE DEFECT ITSELF: comparing the hub's pair as raw strings
        returns the STALE receipt's stamp as the greater, while parsing them returns the
        fresher one.  Without (3) the fractional row would only prove that the hook refuses
        a fraction, not that refusing it was necessary.
        """
        self.assertRegex(CONTROL_STAMP_WHOLE, PINNED_UTC_RX)
        self.assertRegex(CONTROL_STAMP_LATER, PINNED_UTC_RX)
        self.assertLess(
            _pinned_moment(CONTROL_STAMP_WHOLE), _pinned_moment(CONTROL_STAMP_LATER)
        )
        for stamp in CONTROL_STAMPS_REFUSED:
            self.assertIsInstance(stamp, str)
            self.assertTrue(stamp, "a refused stamp must still be a NON-EMPTY string")
            self.assertIsNone(PINNED_UTC_RX.match(stamp), stamp)
            self.assertIsNone(_pinned_moment(stamp), stamp)
        # The sixth commit's answer, and the reason this delta exists: `Z` is 0x5A and `.`
        # is 0x2E, so the WHOLE-second stamp sorts ABOVE the later fractional one.
        self.assertGreater(CONTROL_STAMP_WHOLE, CONTROL_STAMP_FRACTION)
        self.assertLess(CONTROL_STAMP_WHOLE[:19], CONTROL_STAMP_FRACTION[:19] + ".")

    def test_every_stamp_the_default_receipt_fixture_writes_carries_the_pinned_notation(self):
        """O141: the table's own fixtures obey the notation they make the hook enforce.

        Every `recordedUtc` written by the DEFAULT receipt fixtures -- the five kill-switch
        receipts, both execution-control receipts and the one-shot enable receipt -- is
        checked against the pinned notation.  A rule the fixtures themselves break is a rule
        whose ALLOW rows are passing for the wrong reason, and the four deliberate near
        misses live in `CONTROL_STAMPS_REFUSED` and in the O141 fixtures alone.
        """
        fixture_receipts_all_six_plus_enable(self.paths)
        stamped = 0
        for name in sorted(os.listdir(self.paths["RECEIPTS"])):
            with open(os.path.join(self.paths["RECEIPTS"], name), "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
            stamp = document.get("recordedUtc")
            if stamp is None:
                continue
            stamped += 1
            self.assertRegex(stamp, PINNED_UTC_RX, name)
            self.assertIsNotNone(_pinned_moment(stamp), name)
        self.assertGreaterEqual(stamped, 3, "the fixture must stamp receipts at all")
        # And the literal the table sends by default carries it too, on both keys.
        document = json.loads(ENABLE_LITERAL)
        for key in ("enabledUtc", "recordedUtc"):
            self.assertRegex(document[key], PINNED_UTC_RX)

    def test_the_ordered_fixture_offers_exactly_one_newest_by_the_parsed_value(self):
        """O141: the ALLOW/DENY ordering pair rests on a fixture that really has an order.

        `_bind_the_enable_literal` falls back to the newest when only one conforming receipt
        exists, so an ordering fixture that lost a receipt would turn the ALLOW row into a
        comparison of a name against itself.  Both receipts are therefore asserted present,
        both conforming, one second apart, and bound the way the two rows assume.
        """
        fixture_control_stamps_ordered_at_board(self.paths)
        self._bind_the_enable_literal()
        self.assertEqual(self.paths["NEWEST_CONTROL"], "execution-control-0.7.json")
        self.assertEqual(self.paths["OLDER_CONTROL"], "execution-control-0.6.json")
        stamps = {}
        for name in (self.paths["NEWEST_CONTROL"], self.paths["OLDER_CONTROL"]):
            path = os.path.join(self.paths["RECEIPTS"], name)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "rb") as handle:
                stamps[name] = json.loads(handle.read().decode("utf-8"))["recordedUtc"]
        self.assertEqual(stamps[self.paths["OLDER_CONTROL"]], CONTROL_STAMP_WHOLE)
        self.assertEqual(stamps[self.paths["NEWEST_CONTROL"]], CONTROL_STAMP_LATER)

    def test_the_o159_fixture_rewrites_the_parity_receipt_without_shrinking_it(self):
        """O159: the repair fixture models the procedure it claims to -- complete, non-shrinking.

        Three claims, asserted rather than described.  (1) The rewritten
        `0.18-roadmap-parity.json` is LONGER than the conforming original it replaced, so the
        fixture models the register's non-shrinking rewrite and nothing a shrink guard would
        refuse.  (2) After the complete repair every chain receipt from 0.35 on carries the
        digest of the REWRITTEN file with `queueSha256` and `productLiveCount` as they were,
        and 0.1 carries no provenance at all.  (3) The partial variant differs in exactly one
        receipt -- 0.35, which still carries the ORIGINAL digest -- so its DENY row is
        attributable to O152's strict rule and not to a second fault.
        """
        original = json.dumps(_receipt_payloads(self.paths)[ROADMAP_PARITY_RECEIPT], indent=2)
        original_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        parity_path = os.path.join(self.paths["RECEIPTS"], ROADMAP_PARITY_RECEIPT)

        fixture_o159_repaired_chain_at_board(self.paths)
        with open(parity_path, "rb") as handle:
            repaired = handle.read()
        self.assertGreater(len(repaired), len(original.encode("utf-8")))
        repaired_sha = hashlib.sha256(repaired).hexdigest()
        self.assertNotEqual(repaired_sha, original_sha)
        for step in CHAIN_STEPS:
            with open(os.path.join(self.paths["RECEIPTS"], _control_name(step)), "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
            if step in CHAIN_PROVENANCE_EXEMPT:
                self.assertNotIn("roadmapParityReceiptSha256", document)
                continue
            self.assertEqual(document["roadmapParityReceiptSha256"], repaired_sha, step)
            self.assertEqual(document["queueSha256"], _derived_sha256("queue:" + step), step)
            self.assertEqual(document["productLiveCount"], 15, step)

        shutil.rmtree(self.paths["RECEIPTS"])
        os.makedirs(self.paths["RECEIPTS"])
        fixture_o159_partial_repair_at_board(self.paths)
        with open(parity_path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), repaired_sha)
        for step in CHAIN_STEPS:
            if step in CHAIN_PROVENANCE_EXEMPT:
                continue
            with open(os.path.join(self.paths["RECEIPTS"], _control_name(step)), "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
            expected = original_sha if step == "0.35" else repaired_sha
            self.assertEqual(document["roadmapParityReceiptSha256"], expected, step)

    def test_every_chain_receipt_the_fixture_writes_carries_two_different_shas_bound_to_the_reviewed_one(self):
        """S123: the fixtures model the NORMAL post-merge shape, and the two ALLOW rows are what they claim.

        Three claims, asserted rather than described.  (1) Every chain receipt the whole-chain
        fixture writes carries BOTH `reviewedHeadSha` and `mergeSha`, each 40 lowercase hex,
        and they DIFFER -- a GitHub merge lands a different commit, so a fixture whose two
        shas coincided would leave every ALLOW row green against a hook that bound the
        verdict to the wrong one.  (2) The verdict each receipt names is the APPROVE of the
        REVIEWED head and mentions the merge sha nowhere: `reviewedHeadSha` is the subject
        the fixture wrote the verdict at, `mergeSha` is not.  (3) The explicit-difference
        fixture's 0.7 carries a merge sha that is neither the default nor the head, and the
        equality fixture's 0.7 carries the SAME value in both keys -- so the one row measures
        the binding and the other measures equality, and neither is a typo.
        """
        fixture_chain_all_six_names_at_board(self.paths)
        for step in CHAIN_STEPS:
            with open(os.path.join(self.paths["RECEIPTS"], _control_name(step)), "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
            reviewed = document["reviewedHeadSha"]
            merge = document["mergeSha"]
            self.assertRegex(reviewed, r"\A[0-9a-f]{40}\Z", step)
            self.assertRegex(merge, r"\A[0-9a-f]{40}\Z", step)
            self.assertNotEqual(reviewed, merge, step)
            self.assertEqual(reviewed, _head_sha(step), step)
            verdict_path = os.path.join(
                self.paths["BOARD"], document["solVerdictPath"].replace("/", os.sep)
            )
            with open(verdict_path, "r", encoding="utf-8") as handle:
                verdict_text = handle.read()
            self.assertIn('"subject_sha": "%s"' % reviewed, verdict_text, step)
            self.assertNotIn(merge, verdict_text, step)

        shutil.rmtree(self.paths["RECEIPTS"])
        os.makedirs(self.paths["RECEIPTS"])
        fixture_control_merge_sha_differs_from_reviewed_head(self.paths)
        with open(os.path.join(self.paths["RECEIPTS"], _control_name("0.7")), "rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
        self.assertRegex(document["mergeSha"], r"\A[0-9a-f]{40}\Z")
        self.assertNotEqual(document["mergeSha"], document["reviewedHeadSha"])
        self.assertNotEqual(document["mergeSha"], _merge_sha("0.7"), "explicit, not the default")

        shutil.rmtree(self.paths["RECEIPTS"])
        os.makedirs(self.paths["RECEIPTS"])
        fixture_control_merge_sha_equals_reviewed_head(self.paths)
        with open(os.path.join(self.paths["RECEIPTS"], _control_name("0.7")), "rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
        self.assertEqual(document["mergeSha"], document["reviewedHeadSha"])
        self.assertEqual(document["mergeSha"], _head_sha("0.7"))

    def test_the_o159_cascade_fixture_preserves_every_original_stamp_and_the_tie_variant_restamps_two(self):
        """O171: the ALLOW cascade keeps each rewritten receipt's ORIGINAL `recordedUtc`; the tie variant does not.

        Two claims, asserted rather than described.  (1) After the complete repair EVERY chain
        receipt -- rewritten or not -- carries the stamp the chain was first written with, so
        the parsed order is unchanged and the ALLOW row passes for the register's reason and
        not because the rewrite happened to land in ascending seconds.  (2) The tie variant
        differs in exactly the two stamps it restamps: 0.6 and 0.7 carry ONE conforming stamp
        later than either original, and every other receipt keeps its original -- so its
        DENY row is attributable to the tie arm alone.
        """
        fixture_o159_repaired_chain_at_board(self.paths)
        for step in CHAIN_STEPS:
            self.assertEqual(self._chain_document(step)["recordedUtc"], _chain_stamp(step), step)

        shutil.rmtree(self.paths["RECEIPTS"])
        os.makedirs(self.paths["RECEIPTS"])
        fixture_o159_cascade_restamped_to_a_tie_at_board(self.paths)
        self.assertRegex(O159_RESTAMP, PINNED_UTC_RX)
        for step in CHAIN_STEPS:
            stamp = self._chain_document(step)["recordedUtc"]
            if step in ("0.6", "0.7"):
                self.assertEqual(stamp, O159_RESTAMP, step)
                self.assertGreater(
                    _pinned_moment(stamp), _pinned_moment(_chain_stamp(step)), step
                )
            else:
                self.assertEqual(stamp, _chain_stamp(step), step)

    def test_every_chain_receipt_the_fixture_writes_carries_a_proof_of_its_own_shas_and_hashes(self):
        """O172: the fixtures' proofs are DERIVED, and the canonicalisation is asserted from scratch.

        Three claims.  (1) Every chain receipt of the whole-chain fixture carries
        `fixedSetEqualityProof` whose first sha is its `reviewedHeadSha`, whose second is its
        `mergeSha`, and whose digest equals a canonicalisation written out HERE, inline,
        rather than by the helper the fixtures use -- the `<sha256>  <path>` lines (two
        spaces), sorted as plain strings, LF-joined with no trailing newline, UTF-8, sha256
        -- so the hook's docstring, the fixture's helper and this line are three readings
        the ALLOW rows force to agree.  (2) The near misses are near misses: 0.35's digest
        really differs from 0.7's, and 0.6's two shas from 0.7's.  (3) The malformed
        fixture's proof differs from the derived one in case alone.
        """
        fixture_chain_all_six_names_at_board(self.paths)
        for step in CHAIN_STEPS:
            document = self._chain_document(step)
            proof = document["fixedSetEqualityProof"]
            self.assertRegex(proof, r"\A[0-9a-f]{40}=[0-9a-f]{40}:[0-9a-f]{64}\Z", step)
            shas, digest = proof.split(":")
            first, second = shas.split("=")
            self.assertEqual(first, document["reviewedHeadSha"], step)
            self.assertEqual(second, document["mergeSha"], step)
            lines = sorted(value + "  " + path for path, value in document["hashes"].items())
            self.assertEqual(
                digest, hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), step
            )
            self.assertEqual(digest, _fixed_set_digest(document["hashes"]), step)
        self.assertNotEqual(
            _fixed_set_digest(_board_hashes(self.paths, "0.7")),
            _fixed_set_digest(_board_hashes(self.paths, "0.35")),
        )
        self.assertNotEqual(_head_sha("0.6"), _head_sha("0.7"))
        self.assertNotEqual(_merge_sha("0.6"), _merge_sha("0.7"))

        shutil.rmtree(self.paths["RECEIPTS"])
        os.makedirs(self.paths["RECEIPTS"])
        fixture_control_equality_proof_malformed(self.paths)
        document = self._chain_document("0.7")
        malformed = document["fixedSetEqualityProof"]
        derived = _equality_proof(
            document["reviewedHeadSha"], document["mergeSha"], document["hashes"]
        )
        self.assertNotEqual(malformed, derived)
        self.assertEqual(malformed.lower(), derived)

    def test_o175_worked_digest_over_a_fixed_three_path_hashes_object(self):
        """O175: a WORKED EXAMPLE, literal end to end.

        The `hashes` object, its canonical `<sha256>  <path>` lines and the expected digest
        are all written out here rather than derived -- the digest was computed ONCE by the
        pinned command,
        `python -c "import hashlib,sys,json;h=json.load(open(sys.argv[1]))['hashes'];
        print(hashlib.sha256('\\n'.join(sorted(h[k]+'  '+k for k in h)).encode()).hexdigest())"
        <receipt>`, against a receipt carrying exactly this `hashes` object, and pasted below
        as a literal.  The assertion is against the HOOK's own canonicalisation function,
        loaded by `_load_hook_module` -- not the suite's independent restatement
        (`_fixed_set_digest` above) that every other test in this file uses -- because the
        whole point of O175 is that the register's sentence and the hook's code must agree,
        and a comparison against the suite's own second implementation would prove nothing
        about the hook at all.
        """
        hashes = {
            "tools/hooks/mlv-never-authorized.py": (
                "9e04fdac339ac81a80705d7b544ed99ff7fdef88216e0f76ac3567a408aada17"
            ),
            "tools/repo_hygiene/test_mlv_never_authorized.py": (
                "62ae321defcdf5f970a29c92c47cbdff27a0bb435a875196247d3c11dc61fef3"
            ),
            "tools/hooks/test_registration_path_local.py": (
                "8f6e02b42a2e89d22a13c80e71d596f7315851d133f7c9ff66243b5cada04142"
            ),
        }
        # The three `<sha256>  <path>` lines (two spaces), sorted as plain strings -- written
        # out so the ordering itself is a literal, not merely the digest at the end of it.
        expected_lines = [
            "62ae321defcdf5f970a29c92c47cbdff27a0bb435a875196247d3c11dc61fef3  "
            "tools/repo_hygiene/test_mlv_never_authorized.py",
            "8f6e02b42a2e89d22a13c80e71d596f7315851d133f7c9ff66243b5cada04142  "
            "tools/hooks/test_registration_path_local.py",
            "9e04fdac339ac81a80705d7b544ed99ff7fdef88216e0f76ac3567a408aada17  "
            "tools/hooks/mlv-never-authorized.py",
        ]
        self.assertEqual(sorted(value + "  " + path for path, value in hashes.items()), expected_lines)
        # Computed ONCE, by hand, with the pinned command against a receipt carrying exactly
        # this `hashes` object -- pasted here as a literal, never re-derived by this test.
        expected_digest = "bac6f11665439b302c323725ac968299cd919ecac6e45c5c3b9d46d82d6e2327"
        self.assertEqual(
            hashlib.sha256("\n".join(expected_lines).encode("utf-8")).hexdigest(),
            expected_digest,
        )
        hook_module = _load_hook_module()
        self.assertEqual(hook_module._fixed_set_digest(hashes), expected_digest)

    def test_o175_the_pinned_command_and_the_hook_agree_on_a_fixture_receipt(self):
        """O175: the plan's CANONICAL COMMAND, run as a real subprocess, against the hook's own function.

        A structural cross-check beside the worked example above: the register's
        `fixedSetEqualityProof` sentence names an exact command line, and this test runs
        THAT TEXT -- unmodified, as a subprocess against a receipt written to disk -- and
        asserts its stdout equals what the hook's own `_fixed_set_digest` computes for the
        same `hashes`.  The command and the code are two independent readings of the same
        paragraph; a hub that skipped running the assertion, or a hook that silently drifted
        from the paragraph, would separate them, and this is what would catch it.
        """
        hashes = _board_hashes(self.paths, "0.7")
        receipt_path = os.path.join(self.paths["BOARD"], "o175-worked-digest-receipt.json")
        _write(receipt_path, json.dumps({"hashes": hashes}))
        pinned_command = (
            "import hashlib,sys,json;h=json.load(open(sys.argv[1]))['hashes'];"
            "print(hashlib.sha256('\\n'.join(sorted(h[k]+'  '+k for k in h))"
            ".encode()).hexdigest())"
        )
        completed = subprocess.run(
            [sys.executable, "-c", pinned_command, receipt_path],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        pinned_command_digest = completed.stdout.strip()
        self.assertRegex(pinned_command_digest, r"\A[0-9a-f]{64}\Z")
        hook_module = _load_hook_module()
        self.assertEqual(hook_module._fixed_set_digest(hashes), pinned_command_digest)

    def test_settings_json_registers_the_hook_structurally_on_hosted_ci(self):
        """Sol's PR #77 review BLOCKER: hosted CI never proves the hook is REGISTERED.

        `tools/hooks/test_registration_path_local.py` is deliberately local-only (it pins
        an absolute per-user interpreter path that exists on no hosted runner -- see its own
        module docstring), and hosted CI's `unittest discover -s tools/repo_hygiene` never
        collects it. So a PR that reverted or deleted the `.claude/settings.json` PreToolUse
        entry entirely -- the load-bearing wiring, not the rule LOGIC this suite otherwise
        tests -- would still show every hosted required check green. Reproduced: `git grep`
        for `unittest discover` in `.github/workflows/tests.yml` shows `-s tools/repo_hygiene
        -p "test_*.py"`, which does not reach `tools/hooks/`.

        This test cannot prove the interpreter is correct on this host (that is exactly what
        the local-only suite proves, and hosted runners do not carry that per-user path) --
        but it CAN prove, portably, that the registration entry exists at all, with the exact
        matcher and type, naming the right script and carrying the load-bearing
        `--project-dir` venue argument (O126). That is precisely the shape a careless merge
        or an accidental settings.json edit would break, and it is exactly what hosted CI was
        blind to before this test existed.
        """
        settings_path = os.path.join(REPO_ROOT, ".claude", "settings.json")
        with open(settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        pretooluse = settings.get("hooks", {}).get("PreToolUse", [])
        matches = [
            hook
            for entry in pretooluse
            if entry.get("matcher") == "Bash|PowerShell|Write|Edit|NotebookEdit"
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
            and "tools/hooks/mlv-never-authorized.py" in hook.get("command", "")
        ]
        self.assertEqual(
            len(matches),
            1,
            "expected exactly one command-type hook on the exact matcher naming the "
            "never-authorized script; found %d in %s" % (len(matches), settings_path),
        )
        command = matches[0]["command"]
        self.assertIn(
            '--project-dir "${CLAUDE_PROJECT_DIR}"',
            command,
            "the load-bearing venue argument (O126) must be present verbatim",
        )
        self.assertRegex(
            command,
            r'^"[^"]*python[^"]*\.exe"\s+"\$\{CLAUDE_PROJECT_DIR\}/tools/hooks/mlv-never-authorized\.py"',
            "interpreter must be a quoted absolute path ending in python.exe, and the script "
            "path must be built from ${CLAUDE_PROJECT_DIR} (never a literal absolute path, "
            "which would silently stop matching after a worktree move)",
        )

    def test_is_absolute_recognises_posix_and_windows_paths_alike(self):
        """NA-7: the absolute-path test must not be Windows-only.

        Found by hosted CI on ubuntu-latest, invisible on this suite's own local (Windows)
        runs: `is_absolute` matched only a drive-letter path (`c:/...`) or a UNC path
        (`//...`), so a genuine POSIX absolute path (`/tmp/x`) read as "not absolute" and
        `_na7_check_path`'s early return ("a relative destination resolves inside the
        worktree by construction") fired on it -- NA-7 never denied a write to a real
        absolute path outside the worktree/board roots on a non-Windows host. The `r1 write
        into another project tree` / `r2 shell write outside the repo` rows exercise this
        end-to-end via an OS-native tempdir path and so only ever stressed ONE path style
        per host; this test checks both styles directly, on every host, regardless of which
        style the local OS would have produced.
        """
        hook_module = _load_hook_module()
        norm = hook_module.norm
        is_absolute = hook_module.is_absolute
        self.assertTrue(is_absolute(norm("/tmp/outside/stray.txt")), "POSIX absolute path")
        self.assertTrue(is_absolute(norm("c:/users/x/outside/stray.txt")), "drive-letter path")
        self.assertTrue(is_absolute(norm("//server/share/outside/stray.txt")), "UNC path")
        self.assertFalse(is_absolute(norm("outside/stray.txt")), "relative path")
        self.assertFalse(is_absolute(norm("../outside/stray.txt")), "relative parent path")

    def _hook_read_env_names(self):
        """The env names the HOOK ITSELF reads, derived from its OWN `env.get(...)` sites.

        The SAME derivation `test_every_environment_input_the_hook_reads_is_na3_protected`
        and `test_every_environment_input_the_hook_reads_is_expanded_in_a_path` perform, and
        for the same reason: a name list spelled out twice is a list that goes stale once, so
        the next input added to `Ctx.__init__` must reach every gate that keys on it without
        anyone remembering to widen a literal tuple.  `MLV_HOOK_DRYRUN` is the one exemption
        and it is named rather than pattern-matched: it is not a ROOT, it selects whether the
        decision is PRINTED, and it can change no decision.
        """
        with open(HOOK, "r", encoding="utf-8") as handle:
            source = handle.read()
        names = set(re.findall(r"env(?:iron)?\.get\(\s*[\"'](\w+)[\"']", source))
        self.assertTrue(names, "the hook reads no environment input at all?")
        return sorted(names - {"MLV_HOOK_DRYRUN"})

    def _assert_no_fixture_binds_a_hook_read_root_outside_tmp(self, cases, fixtures):
        """ROUND 4 (sol, PR #104): the guard must check the OVERRIDES, not just the base bind.

        Round 3 admitted `$env:MLV_BOARD_ROOT` / `{BOARD_NORM}` as fixture anchors and
        justified it by asserting that `_invoke` binds `MLV_BOARD_ROOT` to the TMP board.
        That justification was incomplete, and sol showed why: `_invoke` applies
        `env.update(overrides)` AFTER that binding, so a row's OWN fixture wins.  A case
        whose fixture re-bound `MLV_BOARD_ROOT` to the REAL board would then have spelled the
        real board through an APPROVED anchor and passed the very guard whose entire purpose
        is to catch a fixture pointing at the real board.  The base binding is a default, not
        an invariant, so the invariant is asserted directly here instead: for every name the
        hook READS, no case's fixture may bind it anywhere but inside this test's tmp tree.

        The name list is DERIVED from the hook's own call sites (`_hook_read_env_names`), so
        a new steering root is covered the day it is read -- the recorded case is a root that
        shipped ahead of the gates that should have known it, and was ultimately removed.
        The venue is not among them by construction: it arrives on argv under `VENUE_KEY`,
        never through the environment (O126), and NA-10's `venue_at_worktree` row legitimately
        names the real checkout root there.
        """
        names = self._hook_read_env_names()
        tmp = os.path.normcase(os.path.abspath(self.tmp))
        for fixture_name in sorted(set(case.get("fixture", "default") for case in cases)):
            overrides = fixtures[fixture_name](self.paths)
            for name in names:
                if name not in overrides:
                    continue
                value = overrides[name]
                if value is UNSET:
                    # ROUND 5: an UNSET name binds NOTHING, so there is no fixture path to
                    # check.  What the hook then uses is its own built-in default, which is
                    # the very configuration the paired rows exist to measure -- and it is a
                    # DECISION about that path, never a touch of it.
                    continue
                resolved = os.path.normcase(os.path.abspath(value))
                self.assertTrue(
                    resolved == tmp or resolved.startswith(tmp + os.sep),
                    "fixture %r binds %s to %r, which is OUTSIDE this test's tmp tree (%s) "
                    "-- a fixture's overrides are applied after `_invoke`'s base binding, so "
                    "this would aim the hook at a root on this machine"
                    % (fixture_name, name, value, self.tmp),
                )

    def test_no_case_references_a_real_board_path(self):
        """No case may depend on `.claude-state` or the real board root (O81/O97)."""
        blob = json.dumps(CASES)
        self.assertNotIn("Layi Wkspc", blob)
        self.assertNotIn("bachelor", blob)
        # ROUND 3 (S134): `$env:MLV_BOARD_ROOT` is an ANCHOR beside `{BOARD}`, and ONLY
        # because the name resolves to `paths["BOARD"]` -- the tmp board -- on every row.
        # The gate's claim is unchanged: no case may depend on the REAL board root, and the
        # two literal guards above are what enforce that and are untouched.  What changed is
        # that a row may now spell the fixture's board through the very variable whose
        # expansion it is testing.
        #
        # ROUND 4: the resolution is asserted, not remembered, and in BOTH halves -- the base
        # binding in `_invoke`, asserted against `HARNESS_SOURCE` just below, and the fixture
        # overrides applied after it, which round 3 did not check at all.  The second half is
        # `_assert_no_fixture_binds_a_hook_read_root_
        # outside_tmp` is the second half and covers every root the hook reads, not just this
        # one; `test_the_real_board_guard_catches_a_fixture_that_rebinds_a_read_root` is its
        # falsifier.
        self.assertIn(
            'env["MLV_BOARD_ROOT"] = self.paths["BOARD"]',
            HARNESS_SOURCE,
            "the env anchor below is only safe while `_invoke` binds it to the tmp board",
        )
        self._assert_no_fixture_binds_a_hook_read_root_outside_tmp(CASES, FIXTURES)
        # `{BOARD_NORM}` is the same tmp board as the hook PRINTS it, bound in `setUp` from
        # `paths["BOARD"]` -- a fixture placeholder like `{BOARD}`, never a machine path.
        # ROUND 5: `{DEFAULT_BOARD_NORM}` is the ONE anchor that names the hook's built-in
        # default rather than the tmp board, and it is admitted deliberately.  The `x e` row
        # exists to measure the configuration where `MLV_BOARD_ROOT` is UNSET, and in that
        # configuration the hook judges against `DEFAULT_BOARD_ROOT` -- so a row that could
        # not name it could not assert the expansion resolved at all.  It stays inside this
        # gate's claim: the placeholder is bound in `setUp` from the HOOK MODULE's own
        # constant (never a literal here, and the "Layi Wkspc" guard above still holds over
        # the table text), and the row asserts a REFUSAL about that path -- nothing is
        # written, read or stat'ed there, by the hook or by this suite.
        anchors = (
            "{BOARD}",
            "{BOARD_NORM}",
            "{DEFAULT_BOARD_NORM}",
            "$env:MLV_BOARD_ROOT",
            "${env:MLV_BOARD_ROOT}",
        )
        for match in re.finditer(r"\.claude-state[^\"\\]*", blob):
            window = blob[max(0, match.start() - 40) : match.start() + 1]
            self.assertTrue(
                any(anchor in window for anchor in anchors),
                "a `.claude-state` path must come from the fixture, not this machine: %r"
                % window,
            )

    def test_the_real_board_guard_catches_a_fixture_that_rebinds_a_read_root(self):
        """ROUND 4: sol's OWN construction, run as a falsifier -- it must make the guard FAIL.

        A guard nobody has ever seen fail is a guard nobody has evidence about, and this one
        passed a whole round while blind to exactly this shape.  So the shape is built here:
        a synthetic case whose fixture binds a hook-read root to the REAL board root --
        `REPO_ROOT`, derived from this file's own location, so the falsifier carries no
        machine literal and names the real checkout on either matrix leg.  Measured against
        the round-3 guard before this delta: with that fixture appended to `CASES` the guard
        returned OK, because it read the case TEXT and never the fixture's overrides.

        One synthetic row PER NAME the hook reads, so coverage is proved for the whole
        derived list rather than for `MLV_BOARD_ROOT` alone -- the bus root joining that list
        a round late is why the distinction is not academic here.
        """
        for name in self._hook_read_env_names():
            with self.subTest(name=name):
                fixtures = {"fp1_rebinds_a_real_root": lambda paths, n=name: {n: REPO_ROOT}}
                cases = [{"name": "synthetic rebind", "fixture": "fp1_rebinds_a_real_root"}]
                with self.assertRaises(AssertionError) as caught:
                    self._assert_no_fixture_binds_a_hook_read_root_outside_tmp(cases, fixtures)
                self.assertIn(name, str(caught.exception))
        # And the REAL table passes the same check, so the failures above are attributable to
        # the synthetic rows and not to a guard that refuses everything put in front of it.
        self._assert_no_fixture_binds_a_hook_read_root_outside_tmp(CASES, FIXTURES)

    def test_the_hook_never_reads_the_venue_from_the_environment(self):
        """O126, asserted on the SOURCE as well as on the behaviour.

        The behavioural proof is the `manifest_venue_env_only` row.  This one is structural
        and cheaper to read: no line of the hook may both name `CLAUDE_PROJECT_DIR` and
        touch the environment.  The name is still allowed to appear -- it is one of the five
        O129 names whose PERSISTENT assignment NA-3 denies, and it is named throughout the
        prose as the thing `--project-dir` is substituted FROM.
        """
        with open(HOOK, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        offenders = [
            "%d: %s" % (number, line.strip())
            for number, line in enumerate(lines, 1)
            if "CLAUDE_PROJECT_DIR" in line
            and re.search(r"\bos\.environ\b|\bgetenv\b|\benv\.get\b|\benv\[", line)
        ]
        self.assertEqual(offenders, [], "the venue must arrive on argv, never from env")

    def test_the_hook_refuses_an_unknown_argument(self):
        """An argument this hook does not understand fails CLOSED, on one stderr line.

        `parse_known_args` would have swallowed a mistyped `--project-dir` and left the hook
        running with no venue at all -- a silent revert to "every exception shut", which
        looks exactly like a correctly-configured lane.
        """
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, HOOK, "--project-root", self.paths["BOARD"]],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=self.tmp,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "DENY must print exactly ONE line: %r" % lines)
        self.assertTrue(lines[0].startswith("hook-error:"), lines[0])

    def test_an_empty_venue_argument_is_not_the_board(self):
        """`--project-dir ""` is the missing-argument reading, not a wildcard."""
        payload = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": self._substitute(MANIFEST_PLAN),
                    "content": MANIFEST_LONGER,
                },
            }
        )
        _manifest_files(self.paths)
        completed = self._invoke(payload, {VENUE_KEY: ""})
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("<absent>", completed.stderr)

    def test_dryrun_prints_the_decision_without_changing_the_exit_code(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "codex login"}})
        completed = self._invoke(payload, {"MLV_HOOK_DRYRUN": "1"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("DRYRUN DENY NA-3", completed.stdout)
        allowed = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        completed = self._invoke(allowed, {"MLV_HOOK_DRYRUN": "1"})
        self.assertEqual(completed.returncode, 0)
        self.assertIn("DRYRUN ALLOW", completed.stdout)


PWSH = shutil.which("pwsh")


def _unresolvable_drive_receipt():
    """A receipt path on a drive letter that does not exist on THIS host.

    Pinned to `Q:` when `Q:` is free -- the letter the card names -- and otherwise the first
    free letter walking backwards, so the case still runs on a host with a Q: mapping instead
    of quietly testing a drive that resolves.  Returns None when every letter is taken.
    """
    for letter in ["Q"] + [chr(code) for code in range(ord("Z"), ord("D") - 1, -1)]:
        if not os.path.exists(letter + ":\\"):
            return letter + ":\\definitely-missing\\0.2-loop-enabled.json"
    return None


@unittest.skipIf(
    PWSH is None,
    "pwsh (PowerShell 7+) is not on PATH, so the S101/O128 acceptance cannot be RUN.  It is "
    "the one claim in this suite that is not about the hook's reading of a string but about "
    "what PowerShell actually DOES with the canonical compound, and a simulation of it "
    "would prove nothing: skipped rather than faked.",
)
class EnableCompoundIsFailClosedTests(unittest.TestCase):
    """S101/O128 ACCEPTANCE, EXECUTED FOR REAL.

    Every other case in this file is a string the hook reads.  These five are different in
    kind: the register's whole reason for pinning the shape is a claim about PowerShell's
    RUNTIME -- that ``Set-Content`` fails NON-terminatingly by default, so a rev-18 compound
    would delete the marker with the receipt absent.  A hook rule that rests on an unverified
    runtime claim is a rule resting on a guess, so the compound is executed here against TEMP
    PATHS ONLY.  Nothing touches the board, no receipt of record is written, and the marker
    is a scratch file this test created.

    THE FALSIFIER IS PINNED TO SAME-DRIVE FAILURES (S101), because those exercise the WRITE:
    a read-only target, a missing parent, and a target that is a directory.  The
    UNRESOLVABLE-DRIVE case is kept as its OWN case (O128) precisely because it does not: the
    hub measured that without the leading ``$ErrorActionPreference = 'Stop'`` that path exits
    0 and DELETES THE MARKER, because the provider dynamic parameters never bind and
    ``-ErrorAction`` goes unbound with them.  It is a parameter-BINDING failure, not a write
    failure, which is why a preference variable -- something that cannot come unbound -- is
    what closes it, and why it must be measured separately rather than folded in as a fourth
    write failure.
    """

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlv-s101-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.marker = os.path.join(self.tmp, "WORKSTREAM-LOOP-DISABLED")
        _write(self.marker, "stopped\n")

    def _run(self, receipt):
        compound = _canonical_enable(
            literal=RUNTIME_ENABLE_LITERAL, receipt=receipt, marker=self.marker
        )
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", compound],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.tmp,
        )

    def _assert_halted_before_the_delete(self, receipt, label):
        completed = self._run(receipt)
        detail = "\ncase: %s\nstdout: %r\nstderr: %r" % (
            label,
            completed.stdout,
            completed.stderr,
        )
        self.assertNotEqual(
            completed.returncode, 0, "the compound must fail, not continue" + detail
        )
        self.assertTrue(
            os.path.isfile(self.marker),
            "the kill switch was disarmed by a compound whose receipt write FAILED -- "
            "exactly the state the fail-closed shape exists to make impossible" + detail,
        )
        return completed

    def test_it_halts_at_a_read_only_receipt_and_leaves_the_marker_in_place(self):
        """SAME-DRIVE failure 1: the target exists and cannot be written."""
        receipt = os.path.join(self.tmp, "0.2-loop-enabled.json")
        _write(receipt, "previous contents\n")
        os.chmod(receipt, stat.S_IREAD)
        self.addCleanup(os.chmod, receipt, stat.S_IWRITE | stat.S_IREAD)
        self._assert_halted_before_the_delete(receipt, "read-only receipt")
        with open(receipt, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "previous contents\n", "the receipt was rewritten")

    def test_it_halts_at_a_missing_parent_directory_and_leaves_the_marker_in_place(self):
        """SAME-DRIVE failure 2: the parent directory does not exist."""
        receipt = os.path.join(self.tmp, "no-such-directory", "0.2-loop-enabled.json")
        self._assert_halted_before_the_delete(receipt, "missing parent directory")
        self.assertFalse(os.path.exists(receipt), "no receipt should have been written")

    def test_it_halts_when_the_receipt_path_is_a_directory(self):
        """SAME-DRIVE failure 3: the target resolves, and it is not a file."""
        receipt = os.path.join(self.tmp, "0.2-loop-enabled.json")
        os.makedirs(receipt)
        self._assert_halted_before_the_delete(receipt, "receipt path is a directory")
        self.assertTrue(os.path.isdir(receipt), "the directory should still be a directory")

    def test_it_halts_on_an_unresolvable_drive_and_leaves_the_marker_in_place(self):
        """O128, ITS OWN CASE.  A BINDING failure, not a write failure.

        Measured by the hub against the rev-19 shape: exit 0, no receipt, MARKER DELETED.
        With the leading preference it exits non-zero with the marker in place, which is
        what this asserts -- the one arm that proves the preference statement is doing work
        the three ``-ErrorAction Stop`` arms cannot do.
        """
        receipt = _unresolvable_drive_receipt()
        if receipt is None:
            self.skipTest("every drive letter D:-Z: is in use, so no path can fail to bind")
        self._assert_halted_before_the_delete(receipt, "unresolvable drive %r" % receipt)

    def test_the_happy_path_writes_the_literal_verbatim_and_removes_the_marker(self):
        """The other half: when the write succeeds the enable completes, byte for byte."""
        receipt = os.path.join(self.tmp, "receipts", "0.2-loop-enabled.json")
        os.makedirs(os.path.dirname(receipt))
        completed = self._run(receipt)
        detail = "\nstdout: %r\nstderr: %r" % (completed.stdout, completed.stderr)
        self.assertEqual(completed.returncode, 0, "expected the enable to complete" + detail)
        self.assertTrue(os.path.isfile(receipt), "the receipt was not written" + detail)
        with open(receipt, "rb") as handle:
            written = handle.read()
        # `-NoNewline -Encoding utf8` is why this is byte-exact: PowerShell 7's `utf8` is
        # UTF8NoBOM, and the read-back compares with `-cne`, so any drift would have thrown.
        self.assertEqual(
            written,
            RUNTIME_ENABLE_LITERAL.encode("utf-8"),
            "the receipt is not the literal's bytes" + detail,
        )
        self.assertFalse(
            os.path.exists(self.marker), "the marker should be gone" + detail
        )


# ------------------------------------ NA4-OWNER-CONSENTED-FOOTAGE-1: the consent record
#
# ROUND 6 (NARROW).  Rounds 1-5 tested an exception (3) that admitted a consented path named in
# command text; that admission is removed, and with it every row of its machinery (the
# literal-token allowlist, the whole-shell-word span test, ``is_owner_consented_path``).  The
# frozen ``OWNER_CONSENTED_FOOTAGE`` table stays as the consent record, value-pinned here, and a
# consented-footage path in command text is DENIED by NA-4 like any other footage path.  Every
# row uses synthetic, extension-free names under a synthetic CLIP CACHE root -- NA-4 treats the
# cache root as footage exactly as it treats a clip extension -- and an injected fixture table
# whose path hashes are computed from the hook's own ``norm``, so the DENY rows run against a
# table under which each path WOULD have been consented.  No row reads real footage.

CONSENTED_IDS_PINNED = (
    "M16-1243",
    "M16-1327",
    "M16-1347",
    "M17-1207",
    "M15-1320",
    "M16-1210",
)
# sha256 of the raw JSONL line bytes EXCLUDING the line terminator.
OWNER_CONSENT_LINE = 1739
OWNER_CONSENT_LINE_SHA256 = "461af7a5291099e009230a8abcfe547e064435287a23f40cf93e5ba4ddb0a066"
OWNER_CONSENT_QUESTION_LINE = 1718
OWNER_CONSENT_QUESTION_LINE_SHA256 = (
    "88484275b4ef158bdaf0a98a3755c70fb65be34c514ae406c6603446dee4a38a"
)
OWNER_DIRECTIVE_LINE = 2137
OWNER_DIRECTIVE_LINE_SHA256 = "254075c85a821758067f7d37dbc5a26a05ae8e2a2368f1e02af0fa2110ac8e41"
REGISTER_DOC = os.path.join(REPO_ROOT, "docs", "never-authorized.json")
# ROUND 3 (A; sol BLOCKER, fable MAJOR).  The VALUES of the frozen table, pinned: the sha256 of
# `_canonical_consent_table` over the WHOLE table (ids, per-part length, content sha256 and
# path_norm_sha256, part counts, purposes and citations), and each id's part count.  An
# in-place hash swap or an appended part row changes the digest; the latter also a count.
OWNER_CONSENT_TABLE_SHA256 = "38a4e60b7f4e1a484d361d175d839314c87d49063adb5760ac3a9f07aa9c860a"
OWNER_CONSENT_PART_COUNTS = {
    "M16-1243": 2,
    "M16-1327": 2,
    "M16-1347": 2,
    "M17-1207": 2,
    "M15-1320": 2,
    "M16-1210": 2,
}


def _plain_consent_value(value):
    """Mapping proxies and tuples -> plain dicts and lists, so the table serialises as JSON."""
    if hasattr(value, "items") and hasattr(value, "keys"):
        return {str(key): _plain_consent_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_consent_value(item) for item in value]
    return value


def _canonical_consent_table(table):
    """-> the canonical UTF-8 serialisation the pinned digest is taken over."""
    return json.dumps(
        _plain_consent_value(table), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _normalised_claim_prose(text):
    """-> one lowercase line: ``#`` markers dropped, every whitespace run collapsed to a space.

    ROUND 8.  Round 6's overclaim survived round 7 inside ``rule_na4``'s own comment as the
    PARAPHRASE "reached only through", which walked straight past a literal, case-sensitive
    anti-needle for "reachable ONLY through" -- and it was split across two ``#`` lines, so even
    the right spelling would have needed the right wrap point.  Normalising first removes all
    three escapes at once: case, line wrapping and the comment marker.
    """
    flat = re.sub(r"(?m)^[ \t]*#[ \t]?", " ", text)
    return re.sub(r"\s+", " ", flat).lower()


# One exclusivity claim about consented footage: the SUBJECT, then ``only``, then a route
# preposition, all inside one sentence.  A literal ``.`` ends the window, which is deliberately
# conservative -- a period inside a path (``verify_consented_footage.py``) or a version number
# closes it early, so this UNDER-matches rather than firing on honest neighbouring prose.
_EXCLUSIVE_ROUTE_CLAIM_RX = re.compile(
    r"\bconsented[ -](?:footage|clips?|parts?|paths?)\b"
    r"(?:(?!\.)[\s\S]){0,140}?"
    r"\bonly\b"
    r"(?:(?!\.)[\s\S]){0,60}?"
    r"\b(?:through|via|by|with|using|from)\b"
)

# Spellings that assert exclusivity without naming the subject nearby, so the regex above
# cannot see them.  Matched against NORMALISED prose, hence all lowercase and single-spaced.
_EXCLUSIVE_ROUTE_PHRASINGS = (
    "reachable only through", "reachable only via", "reachable only by",
    "reached only through", "reached only via", "reached only by",
    "accessible only through", "accessible only via",
    "available only through", "available only via",
    "opened only through", "opened only via", "opened only by",
    "only reachable through", "only reached through", "only reachable via",
    "only through tracked", "only via tracked", "only by tracked",
    "only through id-addressed", "only via id-addressed", "only by id-addressed",
)


def _assert_no_exclusive_route_claim(case, text, label):
    """Fail if ``text`` claims consented footage is reachable ONLY through the card's route.

    WHAT THIS CATCHES: the claim re-cased, re-wrapped across
    comment lines, or reworded with one of the subject/verb/preposition combinations
    enumerated above ("reached only via", "a consented clip is opened only by tracked
    consumers"), plus the bounded subject/``only``/route-preposition regex.
    WHAT IT DOES NOT CATCH: it is a phrasing family, NOT a paraphrase detector.  This is not a
    caveat on an otherwise-complete check -- an arbitrary paraphrase of the false claim passes,
    and ROUND 9 pins four that do, by execution, at
    ``test_the_anti_needle_does_not_catch_an_arbitrary_paraphrase``: "the sole route for
    consented footage is...", "a consented clip cannot be opened except by...", "there is no
    other path for...", "nothing besides the verifier can reach...".  A claim whose subject is
    reworded ("owner-consented media", "the six ids") escapes both the list and the regex, as
    does one whose subject and ``only`` are separated by a period or by more than 140
    characters.  It raises the COST of the overclaim; it does not make the overclaim
    impossible, and no finite list of spellings could.
    """
    prose = _normalised_claim_prose(text)
    for phrasing in _EXCLUSIVE_ROUTE_PHRASINGS:
        case.assertNotIn(
            phrasing, prose, "%s: exclusive-route claim %r -- the route is NOT the only way "
            "in; NA-4 exception (2) still admits a consented clip's own path" % (label, phrasing)
        )
    match = _EXCLUSIVE_ROUTE_CLAIM_RX.search(prose)
    case.assertIsNone(
        match,
        "%s: exclusive-route claim %r -- the route is NOT the only way in; NA-4 exception (2) "
        "still admits a consented clip's own path" % (label, match.group(0) if match else ""),
    )


# ---------------------------------------------------------------- derived carrier coverage
#
# ROUND 9.  Round 8 ran the anti-needle over a HAND-MAINTAINED list of four surfaces and was
# one site short: `tools/profiling/bachelor/playback-attr-3-cuda-job.ps1` carries the same
# claim surface and was uncovered, so rewording ITS header back to the false claim would have
# left the whole suite green.  That is the EXACT defect shape of round 7 reappearing inside
# the mechanism built at round 8 to prevent it -- which is the argument against hand lists,
# not against that particular list.
#
# So coverage is DERIVED: scan the tree for files carrying the claim SUBJECT, and run the
# needle over whatever comes back.  Nothing is enumerated by hand, so the specific way round
# 8's list rotted -- somebody forgetting to add a file -- is not how this one rots.
#
# ROUND 10.  That is a CHANGE of failure mode, not the removal of one, and round 9 wrote the
# stronger sentence ("a new carrier cannot be silently uncovered").  What the walk actually
# does: `_derive_claim_carriers` is a BOUNDED filesystem walk, and a carrier that lands in
# any of its bounds is absent from the derived set without a word.
#
# ROUND 11.  Round 10 wrote "FOUR `continue` paths ... all four, named", and BOTH halves of
# that were wrong: (b) below is a `dirnames[:]` filter rather than a `continue` and was
# counted into that total, and the `os.walk` defaults tagged (f) and (g) below skip carriers
# and appeared in NO disclosure, here or in the PR body.  So the closure framing is GONE.
#
#   (a) [continue] its extension is not in `_CARRIER_SUFFIXES` -- a `.rst`, `.adoc` or
#       extensionless carrier is invisible;
#   (b) [dirnames filter, NOT a `continue`] it is under a pruned directory
#       (`_CARRIER_PRUNE_DIRS`), notably `.claude-state/` and `worktrees/`;
#   (c) [continue] it is larger than `_CARRIER_MAX_BYTES`;
#   (d) [continue] reading it raises `OSError` or `UnicodeDecodeError` -- a non-UTF-8 carrier
#       is skipped;
#   (e) BEFORE any of those, `_CLAIM_SUBJECT_RX` matches a LITERAL subject family, so a file
#       that words the subject differently ("owner-consented media", "the six ids") is not
#       found however plainly it asserts the claim;
#   (f) `os.walk(root)` takes the default `onerror=None`, so an `OSError` raised while
#       SCANNING a directory -- permission denied, a vanished path, a long-path failure --
#       is swallowed and that whole subtree contributes nothing, with no exception and no
#       record.  This is NOT the (d) skip: (d) loses one file and this loses a subtree;
#   (g) `os.walk(root)` takes the default `followlinks=False`, so a carrier reachable only
#       through a symlinked directory is never visited.
# A further bound on (e), which round 10 left unsaid: `_CLAIM_SUBJECT_RX` is matched against
# the file's RAW text, so a carrier whose only subject mention is broken across two lines --
# "consented" ending one comment line and "footage" opening the next -- never matches and the
# needle never runs over it, even though `_normalised_claim_prose` would join those lines if
# it were handed them.  DISCLOSED, NOT FIXED: reflow handling would be a new mechanism, and
# this round adds none.
# `_CLAIM_CARRIERS_PINNED` is NOT a backstop for any of these.  It is an equality test between
# the derived set and a frozen copy of it; a file that never entered the derived set never
# enters either side, so the equality stays true and nothing reds.  The pin catches a new
# carrier THE WALK SEES.  It is blind to precisely what the walk is blind to.
_CLAIM_SUBJECT_RX = re.compile(r"consented[ -](?:footage|clips?|parts?|paths?)", re.I)
_CARRIER_PRUNE_DIRS = frozenset({
    ".git", ".claude-state", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    "worktrees",
})
# Text the claim could plausibly be written in; skip (a) above.
_CARRIER_SUFFIXES = (
    ".py", ".ps1", ".psm1", ".json", ".md", ".txt", ".yml", ".yaml", ".sh", ".cpp", ".h",
    ".hpp", ".c", ".pro", ".qrc", ".ui", ".bat", ".cmd", ".mjs", ".js", ".toml",
)
_CARRIER_MAX_BYTES = 4 * 1024 * 1024


def _derive_claim_carriers(root=REPO_ROOT):
    """-> sorted repo-relative paths of the carriers THIS WALK SEES, by the bounds above.

    Not a hand list.  It is a
    SEARCH, not a census: bounded by the subject regex and by the bounds documented above,
    and a carrier outside those bounds is returned by neither this walk nor the pin it feeds.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _CARRIER_PRUNE_DIRS]
        for name in filenames:
            if not name.lower().endswith(_CARRIER_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > _CARRIER_MAX_BYTES:
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            if _CLAIM_SUBJECT_RX.search(text):
                found.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(found)


# The derived set AS PINNED.  This is not the coverage list -- coverage is whatever the
# derivation returns.  This exists so that a new carrier THE WALK RETURNS fails loudly instead
# of quietly widening the set the needle is asked to trust.  A carrier the walk does not
# return reaches neither side of the equality and fails nothing; see the bounds documented
# above `_derive_claim_carriers`.
_CLAIM_CARRIERS_PINNED = (
    "docs/never-authorized.json",
    "tools/gates/resolve_consented_clip.py",
    "tools/gates/verify_consented_footage.py",
    "tools/hooks/mlv-never-authorized.py",
    "tools/profiling/bachelor/Attr3FootagePresenceJob.psm1",
    "tools/profiling/bachelor/Attr3FootageStageJob.psm1",
    "tools/profiling/bachelor/attr3-footage-presence-job.ps1",
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1",
    "tools/repo_hygiene/test_attr3_footage_presence_job.py",
    "tools/repo_hygiene/test_mlv_never_authorized.py",
    "tools/repo_hygiene/test_verify_consented_footage.py",
)

# The ONE carrier the needle cannot be run over, with the reason.  This module must quote the
# false claim verbatim -- the round-6 header and the round-7 residual are probe INPUTS at
# `test_the_anti_needle_catches_the_round_6_and_round_7_residual_spellings`, and an anti-needle
# that forbade its own test fixtures could never be exercised at all.
_CARRIER_NEEDLE_EXCLUSIONS = {
    "tools/repo_hygiene/test_mlv_never_authorized.py":
        "quotes the false claim as anti-needle probe input; see the `caught` tuple",
}


class OwnerConsentedFootageTests(unittest.TestCase):
    """NA-4 consent record: frozen and value-pinned; a consented path in command text is DENIED."""

    def setUp(self):
        self.module = _load_hook_module()
        self.tmp = tempfile.mkdtemp(prefix="mlv-na4c-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # SYNTHETIC drive-letter roots, never the temp dir: portable across the ubuntu and
        # windows legs.  NA-4 decides on strings alone, so nothing here has to exist.
        self.board = "C:/synthetic-board"
        self.cache = "C:/synthetic-board/cache"
        self.part0 = os.path.join(self.cache, "mirror", "consented", "clip-a.part0")
        self.part1 = os.path.join(self.cache, "mirror", "consented", "clip-a.part1")
        self.unconsented = os.path.join(self.cache, "mirror", "unconsented", "clip-z.part0")
        self.saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for key in list(os.environ):
            if key.startswith("MLV_"):
                del os.environ[key]
        os.environ["MLV_BOARD_ROOT"] = self.board
        os.environ["MLV_CLIP_CACHE_ROOT"] = self.cache
        self.table = self._table({"M16-1243": (self.part0, self.part1)})

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    # ------------------------------------------------------------------ fixtures

    def _path_hash(self, path):
        return hashlib.sha256(self.module.norm(path).encode("utf-8")).hexdigest()

    def _table(self, clips):
        return {
            "clips": {
                clip_id: tuple(
                    (
                        index + 1,
                        hashlib.sha256(b"fixture part %d" % index).hexdigest(),
                        self._path_hash(path),
                    )
                    for index, path in enumerate(paths)
                )
                for clip_id, paths in clips.items()
            },
            "purposes": ("fixture purpose",),
            "authority": {},
        }

    def _decide(self, path, consent="fixture"):
        consent = self.table if consent == "fixture" else consent
        saved = self.module.OWNER_CONSENTED_FOOTAGE
        if consent is not None:
            self.module.OWNER_CONSENTED_FOOTAGE = consent
        try:
            return self.module.decide(
                {"tool_name": "Bash", "tool_input": {"command": 'mlv_dump -v "%s"' % path}},
                None,
            )
        finally:
            self.module.OWNER_CONSENTED_FOOTAGE = saved

    def _decide_command(self, command):
        saved = self.module.OWNER_CONSENTED_FOOTAGE
        self.module.OWNER_CONSENTED_FOOTAGE = self.table
        try:
            return self.module.decide(
                {"tool_name": "Bash", "tool_input": {"command": command}}, None
            )
        finally:
            self.module.OWNER_CONSENTED_FOOTAGE = saved

    def assertNa4Deny(self, decision):
        self.assertEqual(decision[0], 2, decision)
        self.assertTrue(decision[1].startswith("NA-4: "), decision)

    # ---------------------------------------------------------------------- rows

    def test_deny_a_consented_footage_path_in_command_text(self):
        # ROUND 6 (NARROW).  The fixture table consents `self.part0` and `self.part1` -- their
        # norm hashes ARE in it -- and NA-4 still denies each one named in command text, in
        # every spelling, as a whole shell word, alone or joined to a neighbour.  Every row was
        # an ALLOW at d4bd9721 (exception (3)).  This closes the COMMAND-TEXT route only:
        # exception (2) still admits a consented clip's own path on the CLIP_OR_NONE line,
        # with no content check against the table.
        path = self.part0.replace("\\", "/")
        self.assertIn(self._path_hash(self.part0), [p[2] for p in self.table["clips"]["M16-1243"]])
        for spelling in (self.part0, self.part1, self.part1.upper().replace("\\", "/")):
            with self.subTest(spelling=spelling):
                self.assertNa4Deny(self._decide(spelling))
        for command in (
            "probe %s" % path,
            'probe "%s"' % path,
            "probe '%s'" % path,
            'probe "%s";echo done' % path,
            '(probe "%s")' % path,
        ):
            with self.subTest(command=command):
                self.assertNa4Deny(self._decide_command(command))

    def test_m02_1344_is_absent_from_the_frozen_consent_record(self):
        # ROUND 7.  The two NA-4 DENY calls this row used to make were VACUOUS: no hook rule
        # reads the consent table, so they passed for a reason unrelated to M02-1344 and
        # would have passed with the table deleted outright.  What is left discriminates --
        # it goes RED the moment M02-1344 is added to the FROZEN record.
        self.assertNotIn("M02-1344", self.module.OWNER_CONSENTED_FOOTAGE["clips"])

    def test_the_consent_table_does_not_change_the_na4_decision(self):
        # ROUND 8.  This row used to be named ...`_deny_any_token_whose_norm_hash_is_absent`
        # and asserted NOTHING about norm hashes: `setUp` clears every `MLV_*` key, so
        # `authorized_clip` returns None, `allowed` is None, and every DENY here came from
        # rule_na4's DEFAULT branch -- the fourth vacuous row, green with the table deleted.
        #
        # What replaces it discriminates on the property round 6 actually established: NO HOOK
        # RULE READS THE TABLE, so a token whose norm hash IS in the table and one whose norm
        # hash is NOT get the SAME decision, byte for byte, under every table state.  Restore
        # exception (3) -- the admission rounds 1-5 carried -- and `self.part0` becomes an
        # ALLOW while `self.unconsented` stays a DENY, and this row goes red.
        consented_hash = self._path_hash(self.part0)
        absent_hash = self._path_hash(self.unconsented)
        table_hashes = [part[2] for part in self.table["clips"]["M16-1243"]]
        self.assertIn(consented_hash, table_hashes, "fixture precondition: part0 IS consented")
        self.assertNotIn(absent_hash, table_hashes, "fixture precondition: clip-z is NOT")

        empty = {"clips": {}, "purposes": ("fixture purpose",), "authority": {}}
        for label, consent in (("fixture table", "fixture"), ("empty table", empty),
                               ("real frozen table", None)):
            with self.subTest(table=label):
                consented = self._decide(self.part0, consent=consent)
                absent = self._decide(self.unconsented, consent=consent)
                self.assertNa4Deny(consented)
                self.assertNa4Deny(absent)
                # Same exit code and the same reason SHAPE: the two differ only in the path
                # they name, never in whether the table admitted one of them.
                self.assertEqual(consented[0], absent[0])
                self.assertEqual(
                    consented[1].replace(self.module.norm(self.part0), "<PATH>"),
                    absent[1].replace(self.module.norm(self.unconsented), "<PATH>"),
                    "a consented norm hash must not buy a different NA-4 outcome",
                )

    # ------------------------------------------------------------- values pinned (A)

    def test_the_whole_frozen_table_is_value_pinned(self):
        table = self.module.OWNER_CONSENTED_FOOTAGE
        self.assertEqual(
            hashlib.sha256(_canonical_consent_table(table)).hexdigest(),
            OWNER_CONSENT_TABLE_SHA256,
            "the frozen consent table's VALUES changed; a change here is a consent change",
        )
        self.assertEqual(
            {clip_id: len(parts) for clip_id, parts in table["clips"].items()},
            OWNER_CONSENT_PART_COUNTS,
        )

    def test_the_pin_sees_a_swapped_hash_and_an_appended_part(self):
        # The two edits both reviewers named: each would have kept round 2's shape tests green.
        plain = _plain_consent_value(self.module.OWNER_CONSENTED_FOOTAGE)
        pinned = hashlib.sha256(_canonical_consent_table(plain)).hexdigest()
        swapped = json.loads(json.dumps(plain))
        swapped["clips"]["M16-1243"][0][2] = "f" * 64
        appended = json.loads(json.dumps(plain))
        appended["clips"]["M16-1243"].append([1, "a" * 64, "b" * 64])
        for label, edited in (("swapped path hash", swapped), ("appended part", appended)):
            with self.subTest(edit=label):
                self.assertNotEqual(
                    hashlib.sha256(_canonical_consent_table(edited)).hexdigest(), pinned
                )
        self.assertEqual(len(appended["clips"]["M16-1243"]), 3)
        self.assertNotEqual(OWNER_CONSENT_PART_COUNTS["M16-1243"], 3)

    def test_limits_state_the_value_pin_the_residual_and_the_trailing_dot_gap(self):
        with open(self.module.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        for needle in (
            # Round 6 (NARROW): no command-text admission, and the id-only route.
            "this hook admits NO consented path from command text",
            "No rule in this hook reads the table.",
            "L1. THE ROUTE THIS CARD PROVIDES for consented\n"
            "# footage is tracked, id-addressed consumers that verify content against this"
            " table",
            # ROUND 7: the route this card provides is NOT the only way in.  NA-4 exception (2)
            # reaches a consented clip today with no content check; CI holds that in place.
            "THAT IS NOT THE ONLY WAY A\n"
            "# CONSENTED CLIP CAN BE OPENED TODAY.",
            "NA-4 exception (2) admits the ONE canonical path on the\n"
            "# card's CLIP_OR_NONE line, and NOTHING EXCLUDES A CONSENTED CLIP'S OWN PATH"
            " FROM IT",
            # ROUND 8 (minor): the LIMITS block carries two numberings.  They are kept apart,
            # and the rule that keeps them apart is stated where they meet.
            "TWO NUMBERINGS MEET IN THIS BLOCK AND ARE KEPT",
            "the limits below are labelled L1..L6, and the NA-4 exceptions keep their own (1)"
            " and",
            # The honesty statement: scope, not exposure; the interpreter residual stands.
            "L2. THE NARROWING REDUCES\n# SCOPE, NOT EXPOSURE.",
            "The interpreter-one-liner residual is UNCHANGED:",
            "so such a one-liner can open\n# any path, consented or not,",
            "VALUE-pinned by tests",
            "A board-rooted actor can still edit the table transiently:",
            "bounded by the NA-10 venue gate and the 0.05\n# hook-enforced receipt that Invoke-Lane checks.",
            "until then the CUDA job generator refuses every owner-clip id.",
            "L5. NA-4 clip detection does not strip trailing dots or spaces from a clip name outside the\n"
            "# cache",
            "NA4-CLIP-NAME-TRIM-1",
            "L6. NA-4 exceptions (1) and (2) still compare the EXPANDED token,",
            "NA4-EXC12-LITERAL-TOKENS-1",
            # ROUND 8: rule_na4's OWN comment carries the disclosure, beside the `continue`
            # that refutes the overclaim.  Round 7 fixed the header block and left this one.
            "THAT IS NOT THE ONLY WAY A CONSENTED CLIP CAN BE OPENED TODAY: the NA-4"
            " exception (2)\n"
            "        # ``continue`` DIRECTLY ABOVE admits a consented clip's own path on the"
            " CLIP_OR_NONE\n"
            "        # line, with NO content check against the table and by no id-addressed"
            " consumer.",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, source)
        # ROUND 9 (was: "must not come back IN ANY SPELLING").  That was an OVERCLAIM about
        # an anti-overclaim needle, and sol disproved it by execution: four ordinary
        # paraphrases walked straight through.  What this actually catches is the round-6 and
        # round-7 spellings re-cased, re-wrapped or re-worded WITHIN the enumerated phrase
        # family, plus the bounded subject/``only``/preposition regex.  An
        # arbitrary paraphrase passes, by construction and for good: a finite list of
        # forbidden spellings can never be "any spelling", and enlarging the list each time a
        # reviewer finds a new one has no end.  The bound is pinned by execution at
        # ``test_the_anti_needle_does_not_catch_an_arbitrary_paraphrase``.
        _assert_no_exclusive_route_claim(self, source, "hook source")
        verifier = os.path.join(REPO_ROOT, "tools", "gates", "verify_consented_footage.py")
        with open(verifier, "r", encoding="utf-8") as handle:
            _assert_no_exclusive_route_claim(self, handle.read(), "verify_consented_footage.py")
        # The removed machinery is gone, not dormant.
        for removed in (
            "is_owner_consented_path", "is_literal_canonical_token", "is_whole_shell_word",
            "token_spans", "_consented_path_norm_hashes",
        ):
            with self.subTest(removed=removed):
                self.assertFalse(hasattr(self.module, removed), removed)

    def test_the_anti_needle_catches_the_round_6_and_round_7_residual_spellings(self):
        # ROUND 8.  An anti-needle nobody exercised is the same defect as a vacuous DENY row:
        # round 7's literal `assertNotIn("Consented footage is reachable ONLY through", ...)`
        # was green while the claim sat four lines away in another spelling.  This row feeds
        # the anti-needle the ACTUAL round-6 text, the ACTUAL round-7 residual, and several
        # rewordings, and requires each to FAIL -- and requires today's honest text to pass.
        caught = (
            # Round 6, as the header block carried it.
            "# Consented footage is reachable ONLY through tracked, id-addressed consumers.",
            # Round 7's surviving residual at rule_na4:3496, wrapped exactly as it was.
            "        # denied here exactly as on master.  Consented footage is reached only"
            " through tracked,\n"
            "        # id-addressed consumers that verify content against"
            " OWNER_CONSENTED_FOOTAGE.",
            # Rewordings of the same false claim.
            "a consented clip is opened only by tracked, id-addressed consumers",
            "consented footage is accessible only via the verifier",
            "CONSENTED FOOTAGE IS REACHED ONLY THROUGH THE CARD'S ROUTE",
            "consented parts are available only through id-addressed consumers",
        )
        for text in caught:
            with self.subTest(text=text[:60]):
                with self.assertRaises(AssertionError):
                    _assert_no_exclusive_route_claim(self, text, "probe")
        # And the honest disclosures the card actually ships must NOT trip it.
        for text in (
            "THAT IS NOT THE ONLY WAY A CONSENTED CLIP CAN BE OPENED TODAY.",
            "The route this card provides for consented footage is tracked, id-addressed"
            " consumers that verify content against tools/gates/verify_consented_footage.py",
            "a consented clip named there is opened with NO content check against this table"
            " and by no id-addressed consumer, exactly as before this card.",
        ):
            with self.subTest(honest=text[:60]):
                _assert_no_exclusive_route_claim(self, text, "probe")

    def test_the_anti_needle_runs_over_the_carriers_the_walk_returns(self):
        # ROUND 9.  Round 8's needle ran over four HAND-LISTED surfaces and missed
        # `playback-attr-3-cuda-job.ps1`, which carries the same claim surface -- the round-7
        # defect shape, reappearing inside the round-8 mechanism built to prevent it.  Coverage
        # is now DERIVED: the needle runs over whatever the scan returns, not over what
        # somebody remembered to list.
        #
        # ROUND 10.  Round 9 wrote that as "the failure mode it exhibited is not available".
        # One failure mode is traded for another: this row covers THE CARRIERS THE WALK FINDS,
        # and the walk has the bounds documented above `_derive_claim_carriers`.  A carrier in
        # any of them is covered by neither this loop nor the pin.
        carriers = _derive_claim_carriers()
        # FAIL LOUDLY on a new carrier THE WALK RETURNED -- which, quoted on its own, is
        # narrower than "a new carrier": a carrier inside the bounds documented above
        # `_derive_claim_carriers` reaches neither side of this equality and fails nothing.
        # Within that scope: not because a new
        # carrier is wrong, but because it must be looked at, since silently widening the
        # derived set is how a hand list rots without anyone noticing.
        self.assertEqual(
            carriers, list(_CLAIM_CARRIERS_PINNED),
            "the set of files carrying the consented-route claim changed.  Read the new "
            "carrier, confirm it does not assert the false exclusivity claim, then update "
            "_CLAIM_CARRIERS_PINNED.  Do NOT add it to an exclusion list to make this pass.",
        )
        for rel in carriers:
            if rel in _CARRIER_NEEDLE_EXCLUSIONS:
                continue
            with self.subTest(carrier=rel):
                with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as handle:
                    _assert_no_exclusive_route_claim(self, handle.read(), rel)
        # The exclusion is a real hole, so it is named, reasoned, and held to ONE entry: a
        # growing exclusion list would be the hand list again, wearing a different hat.
        self.assertEqual(len(_CARRIER_NEEDLE_EXCLUSIONS), 1)
        self.assertIn("tools/repo_hygiene/test_mlv_never_authorized.py",
                      _CARRIER_NEEDLE_EXCLUSIONS)
        # ROUND 10 (opus).  This is a LANDMARK, and it used to be commented "genuinely
        # covered, not merely listed" -- which is the one thing it does NOT show.  The
        # `assertEqual` four lines up already contains this path, so this line can never fail
        # independently; what it records is that round 8's missed carrier is in the derived
        # set.  COVERAGE comes from the loop above, and the only evidence the loop bites is the
        # mutation that rewords this file's header back to the false claim and reds this row.
        self.assertIn("tools/profiling/bachelor/playback-attr-3-cuda-job.ps1", carriers)

    def test_the_anti_needle_does_not_catch_an_arbitrary_paraphrase(self):
        # ROUND 9.  Round 8 shipped the needle under the heading "the overclaim must not come
        # back IN ANY SPELLING", and the PR body called it something "a reword cannot pass".
        # Both were overclaims ABOUT AN ANTI-OVERCLAIM NEEDLE, and sol disproved them by
        # running four ordinary paraphrases through the helper: all four returned clean.
        #
        # The remedy is the one this card already applied to its own subject matter -- state
        # the limit honestly -- NOT a longer list.  Adding these four spellings and re-claiming
        # completeness would be the same error one round later: a finite list of forbidden
        # spellings can never be "any spelling", and paraphrase has no end.
        #
        # So this row pins the LIMIT ITSELF.  Each paraphrase below asserts the SAME false
        # claim the needle exists to kill, and each MUST pass the needle.  If a later round
        # enlarges the phrase family to swallow one of THESE FOUR STRINGS, this row goes RED.
        # It pins four specific escapes, not the
        # existence of escapes -- a round that widened the family around all four would leave
        # this row green, and the disclosure would then be wrong with nothing to say so.
        escapes = (
            "The sole route for consented footage is the tracked verifier",
            "A consented clip cannot be opened except by an id-addressed consumer",
            "There is no other path for opening consented clips",
            "Nothing besides the verifier can reach a consented clip",
        )
        for text in escapes:
            with self.subTest(escapes=text[:60]):
                # Not assertRaises: the POINT is that nothing is raised.
                _assert_no_exclusive_route_claim(self, text, "paraphrase probe")
        # And the documented bound must actually say so, so the code and the prose cannot
        # drift apart the way the heading and the docstring did at round 8.
        doc = _assert_no_exclusive_route_claim.__doc__
        self.assertIn("NOT a paraphrase detector", doc)
        self.assertIn("no finite list of spellings could", doc)

    def test_no_subprocess_on_the_na4_path(self):
        cases = (self.part0, self.part1, self.unconsented, os.path.join(self.tmp, "x.txt"))
        baseline = [self._decide(path) for path in cases]

        def refuse(*args, **kwargs):
            raise AssertionError("NA-4 must not start a process")

        patched = [
            (subprocess, "Popen"), (subprocess, "run"), (subprocess, "check_output"),
            (os, "system"), (os, "popen"),
        ]
        saved = [(owner, name, getattr(owner, name)) for owner, name in patched]
        for owner, name in patched:
            setattr(owner, name, refuse)
        try:
            refused = [self._decide(path) for path in cases]
        finally:
            for owner, name, value in saved:
                setattr(owner, name, value)
        self.assertEqual(refused, baseline)
        # ROUND 9 (SIXTH vacuous row, found by the ablation at
        # `test_the_discovered_decision_rows_discriminate_on_consent_status`, not by hand).
        # These two
        # lines used to read `assertEqual(baseline[0][0], 2)  # round 6: a consented path is
        # denied too` and `assertEqual(baseline[2][0], 2)`.  Both passed for a reason unrelated
        # to consent: `setUp` clears every `MLV_*` key, so `allowed` is None and EVERY path
        # here is denied by rule_na4's default branch.  Re-point `self.part0` at an unconsented
        # path and both stayed green -- so the trailing comment claimed a consent-dependent
        # property the assertions never tested.
        #
        # What replaces them discriminates the same way :7031 does: the consented and the
        # unconsented path must get the SAME decision shape.  Restore exception (3) and
        # `baseline[0]` becomes an ALLOW while `baseline[2]` stays a DENY, and this goes red.
        #
        # The PRECONDITION is what makes the comparison mean anything, and its absence is what
        # made the old lines vacuous: without it, "these two paths get the same decision" is
        # true of any two paths whatever.
        table_hashes = [part[2] for part in self.table["clips"]["M16-1243"]]
        self.assertIn(self._path_hash(self.part0), table_hashes, "part0 IS consented")
        self.assertNotIn(self._path_hash(self.unconsented), table_hashes, "clip-z is NOT")
        consented, unconsented = baseline[0], baseline[2]
        self.assertNa4Deny(consented)
        self.assertNa4Deny(unconsented)
        self.assertEqual(
            consented[1].replace(self.module.norm(self.part0), "<PATH>"),
            unconsented[1].replace(self.module.norm(self.unconsented), "<PATH>"),
            "a consented norm hash must not buy a different NA-4 outcome on the no-subprocess"
            " path either",
        )
        self.assertFalse(hasattr(self.module, "_read_merged_output_budget"))
        self.assertNotIn("subprocess", vars(self.module))

    def test_the_real_hook_process_denies_a_cache_path_as_a_separate_process(self):
        # ROUND 9 (FIFTH vacuous row -- opus, round 8).  This was named
        # ..._denies_an_unconsented_cache_path and fed `self.part0`, the path every other row
        # in this class calls CONSENTED.  It passed for a reason unrelated to the word
        # "unconsented" in its name: `setUp` clears every `MLV_*` key, so `authorized_clip`
        # returns None and the DENY comes from rule_na4's default branch.  Deleting
        # OWNER_CONSENTED_FOOTAGE outright left it green.
        #
        # KEPT, not deleted: its property is end-to-end -- the REAL hook, as a separate
        # process, exits 2 with an "NA-4: " reason, so the in-process `decide()` rows are not
        # testing a different code path than production.
        #
        # ROUND 12 (sol).  Round 9 wrote that as "end-to-end AND NOTHING ELSE COVERS IT", and
        # the added clause is FALSE.  The table-driven rows at the top of this module run the
        # REAL hook as a separate process through `_invoke`, and `_run_case` asserts
        # returncode 2, exactly one stderr line, and that the line starts with the row's own
        # `na` id -- so "r1 open an unnamed clip" and "na4 same basename different clip",
        # both `"na": "NA-4"` with `"expect": "DENY"`, already assert that the real hook, in
        # a separate process, exits 2 with an "NA-4: " reason.  The clause is DELETED rather
        # than narrowed.
        #
        # ROUND 10 (sol) -- DISCLOSED, NOT FIXED, and here is which and why.  Round 9 renamed
        # this row `..._whatever_its_consent_status` and added the fixture precondition below,
        # which made the vacuity enumeration call it discriminating.  IT IS NOT, and this
        # round did not make it so.  The subprocess loads the hook fresh and reads the REAL
        # frozen table; `self.table` exists only in THIS process and is never passed to the
        # child (the call passes argv, env, `cwd=self.tmp` and a JSON payload on stdin --
        # read the `subprocess.run` below rather than this parenthesis).  Under the
        # child's own table NEITHER `self.part0` NOR `self.unconsented` is consented, so the
        # two iterations below are two UNCONSENTED paths and the row observes exactly one
        # consent status, whatever its name said.
        #
        # ROUND 11 -- WHAT THE PATH-HASH FACT ACTUALLY SUPPORTS.  Round 10 argued: the real
        # table stores sha256 path hashes and no paths, THEREFORE no input the child would
        # call consented can be constructed from it.  The premise is true; the conclusion does
        # not follow from it.  What follows is only that a consented path cannot be RECOVERED
        # FROM THE TABLE, sha256 not being invertible.  It does NOT follow that no such path
        # exists: the six consented clips are real files with real paths, and a row handed one
        # of them WOULD be consented in the child.  What actually blocks that here is narrower,
        # and is a fact about scope rather than an impossibility -- those paths are the owner's
        # footage, deliberately absent from the repo and from this fixture, and materialising
        # one is not something this card does.  A table-injection seam would be the other
        # route, and that is executable hook behaviour this card may not change.  So: NOT FIXED
        # HERE, on scope.  NOT proven unfixable.
        #
        # So the row is moved to `_CONSENT_AGNOSTIC_DECIDERS` below, which is what that escape
        # hatch is for, and it now promises only what it shows: the real hook, in a separate
        # process, denies a cache path.  The two-path loop is kept because it does show that
        # two DIFFERENT cache paths get the same outcome shape from the real process -- which
        # is worth having and is not a consent property.
        # ROUND 10.  The paragraph above is a claim about the child, so it is ASSERTED rather
        # than only written down: under the module's REAL frozen table -- the one the child
        # loads -- NEITHER of these two paths is consented.  If a later round ever makes one of
        # them genuinely consented in the child, this goes RED and the disclosure above has to
        # be rewritten instead of quietly becoming false.
        real_path_hashes = {
            part[2]
            for parts in self.module.OWNER_CONSENTED_FOOTAGE["clips"].values()
            for part in parts
        }
        for label, path in (("part0", self.part0), ("unconsented", self.unconsented)):
            with self.subTest(child_table=label):
                self.assertNotIn(
                    self._path_hash(path), real_path_hashes,
                    "%s is consented under the REAL table, so this row now DOES observe two "
                    "consent statuses in the child -- rewrite the ROUND 10 note and reconsider "
                    "the _CONSENT_AGNOSTIC_DECIDERS entry" % label,
                )
        # The labels below are the PARENT fixture's names for these paths.  The child consents
        # neither; see the ROUND 10 note above, now backed by the assertion just made.
        outcomes = {}
        for label, path in (("consented", self.part0), ("unconsented", self.unconsented)):
            env = dict(os.environ)
            env.pop("CLAUDE_PROJECT_DIR", None)
            env["PYTHONIOENCODING"] = "utf-8"
            payload = json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": 'mlv_dump -v "%s"' % path}}
            )
            refused = subprocess.run(
                [sys.executable, HOOK], input=payload, capture_output=True, text=True,
                encoding="utf-8", env=env, cwd=self.tmp,
            )
            with self.subTest(consent=label):
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertTrue(refused.stderr.startswith("NA-4: "), refused.stderr)
            outcomes[label] = (
                refused.returncode,
                refused.stderr.replace(self.module.norm(path), "<PATH>"),
            )
        # ROUND 10.  These two lines are a PARENT-PROCESS fact and nothing more: under the
        # FIXTURE table, part0's norm hash is present and clip-z's is not.  They say nothing
        # about the child, which never sees this table.  They are kept because they document
        # why the two paths were chosen; they are NOT a precondition that makes the comparison
        # below a consent comparison, and round 9's comment here said they were.
        table_hashes = [part[2] for part in self.table["clips"]["M16-1243"]]
        self.assertIn(self._path_hash(self.part0), table_hashes)
        self.assertNotIn(self._path_hash(self.unconsented), table_hashes)
        self.assertEqual(
            outcomes["consented"], outcomes["unconsented"],
            "the real hook process must give these two cache paths the same outcome shape "
            "(both are UNCONSENTED under the child's own table; the labels are the parent's)",
        )

    # Rows that reach a DECISION but promise nothing about consent status, so the ablation
    # below has nothing to hold them to.
    #
    # ROUND 10 (opus).  Round 9 commented this "an explicit, reasoned list because an empty one
    # would be a lie" while it WAS empty -- a sentence that contradicted the line under it, in
    # the card whose subject is comments that assert more than their code.  It is now non-empty
    # and the sentence is gone.  Adding to this set WEAKENS the enumeration, so an entry is a
    # disclosure, not a pass: it says out loud that this row is not held to the consent
    # property, rather than letting an unearned green say the opposite.
    _CONSENT_AGNOSTIC_DECIDERS = frozenset({
        # The subprocess reads the module's REAL frozen table and never receives `self.table`,
        # so both of its inputs are unconsented in the process that decides and the row cannot
        # observe two consent statuses at all.  Full reasoning at the row itself.
        "test_the_real_hook_process_denies_a_cache_path_as_a_separate_process",
    })

    def test_the_discovered_decision_rows_discriminate_on_consent_status(self):
        # ROUND 9.  FOUR rows of the same vacuity class were found ONE AT A TIME across rounds
        # 6, 7 and 8 (two deleted at round 7, one replaced at round 8, a fifth found by opus at
        # round 8).  Finding them one at a time IS the defect: each round declared the class
        # closed and the next round reopened it.  So this row sweeps the class instead of
        # waiting for a reviewer to hit the next member.
        #
        # THE PROPERTY.  A row is vacuous with respect to consent iff its assertions hold
        # identically when consent status is flipped.  So flip it and see whether the row
        # notices.  A row that stays GREEN under every flip is not testing what its name says,
        # whatever its name says.
        #
        # This found the fifth row independently AND a sixth -- the two consent-flavoured
        # assertions on `test_no_subprocess_on_the_na4_path` -- which three rounds of hand
        # auditing had walked past.  Both are fixed above.
        #
        # ROUND 10 -- WHAT THIS DOES NOT DO.  Round 9 said it "closes the class"; that was the
        # card's own defect, reappearing in the mechanism built to prevent it.  This is a
        # SOURCE-TEXT HEURISTIC over an ablation harness, and it has three escapes:
        #
        #  (i) DISCOVERY IS BY LITERAL SPELLING.  A row counts as a decider iff its own source
        #      contains one of three exact strings (see the predicate below).  `_decide` is a
        #      four-line wrapper over `self.module.decide(...)`, so a row calling
        #      `self.module.decide(...)` directly is never classified as a decider, is never
        #      ablated, and can be vacuous and green indefinitely.  So can one using
        #      `subprocess.Popen` or `check_output` instead of `subprocess.run`.
        #      `_CONSENT_AGNOSTIC_DECIDERS` is no backstop: such a row never reaches the
        #      membership test.  THE REMEDY IS NOT A FOURTH SPELLING -- adding spellings and
        #      re-claiming completeness is the defect with a longer list.
        # (ii) DISCRIMINATION HERE MEANS "THE FIXTURE PRECONDITION BREAKS", NOT "THE ROW'S REAL
        #      ASSERTIONS NOTICE".  Since round 6 removed the only rule that read the table, NO
        #      row's substantive assertions CAN depend on consent -- that is the card's own
        #      finding.  Every surviving decider therefore goes red under ablation because a
        #      `self.assertIn(self._path_hash(self.part0), table_hashes)` line stops holding.
        #      The practical consequence: two lines of that boilerplate satisfy this row around
        #      an otherwise vacuous test.  What this actually detects is a decision row that
        #      does not so much as RECORD the consent status of what it hands the decider.
        #      That is weaker than "the row tests consent", and it is what is on offer.
        #(iii) IT SEES ONLY THIS CLASS.  `vars(type(self))` is `OwnerConsentedFootageTests`; a
        #      vacuous consent row in another class is outside it entirely.
        source_of = {}
        for name, function in vars(type(self)).items():
            if not name.startswith("test_") or name == self._testMethodName:
                continue
            try:
                source_of[name] = inspect.getsource(function)
            except (OSError, TypeError):  # pragma: no cover - source always available here
                self.fail("cannot read the source of %s; the enumeration would be a lie" % name)
        # HEURISTIC, by literal spelling -- escape (i) above.
        # A decision row worded any other way is invisible to it.
        deciders = sorted(
            name for name, src in source_of.items()
            if ("self._decide(" in src or "self._decide_command(" in src
                or "subprocess.run(" in src)
            and name not in self._CONSENT_AGNOSTIC_DECIDERS
        )
        self.assertTrue(deciders, "no decision rows found -- the derivation broke, not the class")
        # ROUND 11.  `_CONSENT_AGNOSTIC_DECIDERS` is an opt-out FROM this enumeration, and it
        # was unpinned while its sibling `_CARRIER_NEEDLE_EXCLUSIONS` has been pinned by a
        # length equality plus a membership check since round 9.  Pinned the SAME WAY, so a
        # second entry has to be argued for in a diff instead of appearing quietly.  This is
        # not a new mechanism: it is the two lines the sibling already runs, applied here.
        # It pins the SIZE and the one member.  It does not make the opt-out safe.
        self.assertEqual(len(self._CONSENT_AGNOSTIC_DECIDERS), 1)
        self.assertIn(
            "test_the_real_hook_process_denies_a_cache_path_as_a_separate_process",
            self._CONSENT_AGNOSTIC_DECIDERS,
        )

        original_setup = type(self).setUp

        def ablate(kind):
            def setUp(case):
                original_setup(case)
                if kind == "UNCONSENT":
                    # The fixture table still consents the ORIGINAL part0/part1; the row is now
                    # handed paths that are NOT in it.  Consent status flipped, and because it
                    # is flipped in the ARGUMENT, this reaches the real-hook subprocess row too.
                    # Deliberately NOT `case.unconsented`: if the flipped path collided with
                    # the row's own unconsented path, a consented-vs-unconsented comparison
                    # would compare a path with itself and pass for a NEW vacuous reason.
                    base = os.path.join(case.cache, "mirror", "unconsented")
                    case.part0 = os.path.join(base, "clip-q.part0")
                    case.part1 = os.path.join(base, "clip-q.part1")
                else:  # EMPTYTABLE: the frozen module table consents nothing.
                    case.module.OWNER_CONSENTED_FOOTAGE = {
                        "clips": {}, "purposes": ("fixture purpose",), "authority": {},
                    }
            return setUp

        def run_under(name, kind):
            type(self).setUp = ablate(kind)
            try:
                suite = unittest.TestLoader().loadTestsFromName(name, type(self))
                with open(os.devnull, "w") as sink:
                    return unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
            finally:
                type(self).setUp = original_setup

        vacuous = []
        for name in deciders:
            noticed = [
                kind for kind in ("UNCONSENT", "EMPTYTABLE")
                if not run_under(name, kind).wasSuccessful()
            ]
            with self.subTest(row=name):
                if not noticed:
                    vacuous.append(name)
                self.assertTrue(
                    noticed,
                    "%s decides on a path but is GREEN under BOTH consent-status ablations: "
                    "its assertions do not depend on consent at all. Make it discriminate (see "
                    "test_the_consent_table_does_not_change_the_na4_decision for the shape), "
                    "delete it, or -- if it genuinely promises nothing about consent -- add it "
                    "to _CONSENT_AGNOSTIC_DECIDERS with a reason." % name,
                )
        self.assertEqual(vacuous, [], "the vacuity class is not empty: %s" % vacuous)

    def test_the_frozen_consent_table_is_exactly_the_six_consented_ids(self):
        table = self.module.OWNER_CONSENTED_FOOTAGE
        self.assertEqual(len(table["clips"]), 6)
        self.assertEqual(sorted(table["clips"]), sorted(CONSENTED_IDS_PINNED))
        self.assertNotIn("M02-1344", table["clips"])
        path_hashes = []
        for clip_id, parts in table["clips"].items():
            self.assertTrue(parts, clip_id)
            for part in parts:
                self.assertEqual(len(part), 3, clip_id)
                length, digest, path_hash = part
                self.assertIsInstance(length, int, clip_id)
                self.assertGreater(length, 0, clip_id)
                self.assertRegex(digest, r"\A[0-9a-f]{64}\Z", clip_id)
                self.assertRegex(path_hash, r"\A[0-9a-f]{64}\Z", clip_id)
                path_hashes.append(path_hash)
        self.assertEqual(len(path_hashes), len(set(path_hashes)), "a path hash is listed twice")
        self.assertEqual(
            tuple(table["purposes"]), ("#72b delta-baseline gate", "PLAYBACK-ATTR-3-CUDA")
        )
        with self.assertRaises(TypeError):
            table["clips"]["M02-1344"] = ()  # frozen: a mapping proxy refuses assignment

    def test_the_authority_citations_are_distinct_and_hashed(self):
        authority = self.module.OWNER_CONSENTED_FOOTAGE["authority"]
        self.assertEqual(
            authority["hash_convention"], "sha256 of the raw line bytes excluding the terminator"
        )
        consent = authority["consent"]
        self.assertIn("peaceful-rubin-701ecd", consent["transcript"])
        self.assertIn("a482bbe6-87e2-4332-970e-8accb08b1663.jsonl", consent["transcript"])
        self.assertEqual(consent["line"], OWNER_CONSENT_LINE)
        self.assertEqual(consent["sha256"], OWNER_CONSENT_LINE_SHA256)
        self.assertEqual(consent["answers_line"], OWNER_CONSENT_QUESTION_LINE)
        self.assertEqual(consent["answers_line_sha256"], OWNER_CONSENT_QUESTION_LINE_SHA256)
        directive = authority["directive_no_manual_paths"]
        self.assertIn("f9010ab1-55f2-4f4c-967c-467cf19c04ef.jsonl", directive["transcript"])
        self.assertEqual(directive["line"], OWNER_DIRECTIVE_LINE)
        self.assertEqual(directive["sha256"], OWNER_DIRECTIVE_LINE_SHA256)
        self.assertIn("derived by the hub", authority["scope_derivation"])
        self.assertIn("M02-1344 excluded (not acquired)", authority["scope_derivation"])

    def test_the_frozen_consent_table_matches_the_register_text(self):
        with open(REGISTER_DOC, "r", encoding="utf-8") as handle:
            register = json.load(handle)
        na4 = [item for item in register["items"] if item["id"] == "NA-4"]
        self.assertEqual(len(na4), 1)
        act = na4[0]["act"]
        listed = re.search(r"consented ids \[([^\]]*)\]", act)
        self.assertIsNotNone(listed, "NA-4 act text must list the consented ids")
        ids = [value.strip() for value in listed.group(1).split(",")]
        self.assertEqual(sorted(ids), sorted(self.module.OWNER_CONSENTED_FOOTAGE["clips"]))
        self.assertIn("M02-1344 is NOT consented", act)
        self.assertIn("path_norm_sha256", act)
        self.assertIn("tools/gates/verify_consented_footage.py", act)
        self.assertIn("agent-reviewed and CI-test-pinned", act)
        self.assertIn("NOT yet wired", act)
        # Round 4 (fable minor 2): the register's disclosures are pinned, not only the hook's.
        for needle in (
            "value-pinned (the tests pin a sha256 of the whole canonical table plus each id's part"
            " count)",
            "a board-rooted actor can still edit the table transiently",
            "clip detection does not strip trailing dots or spaces from a clip name outside the"
            " cache (follow-up card NA4-CLIP-NAME-TRIM-1)",
            "until then the CUDA job generator refuses every owner-clip id",
            "exceptions (1) and (2) still compare the expanded token",
            "NA4-EXC12-LITERAL-TOKENS-1",
            # Round 6 (NARROW): no third exception, the id-only route, and the honest residual.
            "there is NO third exception",
            "a consented-footage path named in command text is DENIED exactly like any other"
            " clip outside (1) and (2)",
            "no hook rule reads it",
            "The route this card provides for consented footage is tracked, id-addressed"
            " consumers",
            # ROUND 7: the act must NAME the route that reaches consented footage with no
            # content check, so it agrees with its own LIMITS instead of contradicting them.
            "exception (2) still admits a consented clip's own path on the CLIP_OR_NONE line"
            " with no content check against the table and no id-addressed consumer",
            "the narrowing reduces scope, not exposure: the interpreter-one-liner residual is"
            " unchanged",
            "such a one-liner can open any path, consented or not",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, act)
        self.assertNotIn("fork/master", act)
        self.assertNotIn("exception (3) admits", act)
        # ROUND 8: was two literal, case-sensitive strings; "reached only through" would have
        # passed both.  Now a normalised phrasing family -- see _assert_no_exclusive_route_claim
        # for the bound on what that does and does not cover.
        _assert_no_exclusive_route_claim(self, act, "NA-4 register act")
        for purpose in self.module.OWNER_CONSENTED_FOOTAGE["purposes"]:
            self.assertIn(purpose, act)
        enforced = na4[0]["enforced_after_0_1"]
        self.assertNotIn("exception (3)", enforced)
        self.assertIn(
            "a consented-footage path in command text is denied like any other footage path",
            enforced,
        )
        self.assertIn(
            "the route this card provides for consented footage is tracked, id-addressed"
            " consumers",
            enforced,
        )
        self.assertIn(
            "exception (2) still admits a consented clip's own path on the CLIP_OR_NONE line"
            " with no content check",
            enforced,
        )
        _assert_no_exclusive_route_claim(self, enforced, "NA-4 register enforced_after_0_1")
        for needle in (
            OWNER_CONSENT_LINE_SHA256, OWNER_CONSENT_QUESTION_LINE_SHA256,
            OWNER_DIRECTIVE_LINE_SHA256, "line 1739", "line 1718", "line 2137",
            "excluding the terminator",
        ):
            self.assertIn(needle, register["authority"])
        # Round 1's spec tail is gone with the spec read: the table lives in the hook script.
        self.assertNotIn("tools/gates/output-budget.json", self.module.NA10_GUARDED_TAILS)
        # Round 3 (E): the content verifier IS a gate tail, and the NA-10 register row says so.
        self.assertIn("tools/gates/verify_consented_footage.py", self.module.NA10_GUARDED_TAILS)
        na10 = [item for item in register["items"] if item["id"] == "NA-10"]
        self.assertIn("tools/gates/verify_consented_footage.py", na10[0]["act"])


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _make_test(case):
    def method(self):
        self._run_case(case)

    method.__doc__ = "%s -> %s" % (case["name"], case["expect"])
    return method


_seen = set()
for _case in CASES:
    _name = "test_" + _slug(_case["name"])
    if _name in _seen:
        raise RuntimeError("duplicate table row name: %s" % _case["name"])
    _seen.add(_name)
    setattr(MlvNeverAuthorizedHookTests, _name, _make_test(_case))


if __name__ == "__main__":
    unittest.main()
