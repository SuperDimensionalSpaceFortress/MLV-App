#!/usr/bin/env python3
"""MLV-App PROJECT PreToolUse gate for the never-authorized register (NA-1..NA-10).

Contract
--------
argv   : ``--project-dir <path>`` -- THE VENUE, and nothing else is accepted.
stdin  : exactly ONE JSON object, ``{"tool_name": ..., "tool_input": {...}}``.
exit 0 : ALLOW (nothing on stderr).
exit 2 : DENY, with exactly ONE line on stderr, ``NA-<n>: <reason>``.
exit 2 : fail-CLOSED, with ``hook-error: <detail>`` on stderr, for ANY exception,
         malformed or empty stdin, a non-object payload, a missing ``tool_name``, or an
         UNKNOWN command-line argument (argparse rejects extras; they are never silently
         accepted, because an argument this hook does not understand may be the venue
         under a name it no longer reads).

This is the PROJECT hook described by ``never-authorized.json`` (schema v25) and by
``prompts/v2/card-TOOL-HOOK-ENFORCE-1.md``.  It is NOT the global machine hook
``~/.claude/hooks/check-continuity-boundaries.py``, which is shared by every project on
this machine and fails OPEN.  This one fails CLOSED and is tracked on the same ref as the
tree it guards -- a hook is (interpreter x script x REGISTRATION), and only a tracked
script can travel with the worktree it governs.

THE VENUE RIDES ON ARGV, NOT ON THE ENVIRONMENT (O126)
------------------------------------------------------
``--project-dir`` is substituted by Claude Code from ``${CLAUDE_PROJECT_DIR}`` in the
REGISTERED command string, so its value is fixed by ``.claude/settings.json`` and no tool
call can set it.  The environment variable of that name was MEASURED ABSENT from real hook
processes on this machine (``.hook-invocations.log``: null in 17/17), which is why the
previous revision's venue test -- which read it from ``os.environ`` -- silently never
fired.  This hook therefore NEVER reads ``CLAUDE_PROJECT_DIR`` from its environment; the
name appears below as one of the FIVE NA-3 O129 names whose PERSISTENT assignment is denied,
and as the one of those five held OUT of ``HOOK_READ_ENV_NAMES`` -- a name this hook never
reads has no value here to substitute into a command's text, and inventing one would be the
fabrication S134's residual limit refuses.  Three rules key on the venue, and all three
fail closed when it is missing, a worktree value, or empty -- ABSENT is never "unknown,
assume the hub":

  * NA-2 exception (iv), O124 -- writes (create, extend or amend, ANY length, by
    ``Write``/``Edit``/``NotebookEdit`` or by a shell truncating write) to the four
    ratified manifest surfaces ``<board>/.claude-state/coordination/dual-lane/``
    ``{DEFINITIVE-FIX-PLAN-*.md, never-authorized.json, Start-EditingLane.ps1,
    prompts/v2/**}``.  Delete/move of a surface stays DENIED at every venue.
  * NA-2 exception (i), S105 -- the dedicated 0.2 enable act.  A worktree lane's hook
    evaluates the SAME absolute board paths, so without this test it would admit the
    canonical compound the moment the six receipts exist, before the hub has verified $R
    or installed the task.
  * NA-2, S125 -- the PRE-FLIGHT ARTIFACT act, the SECOND dedicated act: a ``Write`` of
    exactly ``<board>\\.claude-state\\coordination\\dual-lane\\receipts\\``
    ``0.2-enable-preflight-input.json`` is allowed only at the board venue (and only onto
    an absent target, with the exact stdin JSON of a currently valid canonical enable as
    its content -- see THE PRE-FLIGHT ARTIFACT ACT below).
  * NA-10, O129 (hub extension) -- a lane editing its own gate.  Any write to
    ``.claude/settings.json``, ``.claude/settings.local.json`` or
    ``tools/hooks/mlv-never-authorized.py`` or ``tools/gates/verify_consented_footage.py``
    (NA-4's content verifier), UNDER ANY ROOT, is denied away from the board
    venue: this hook is re-read on every tool call, so a lane's edit of its registration or
    of this script would take effect on its next call.  Hook evolution stays hub-authored.

THIS GATE ADMITS NO WRITE TO A SIBLING REPOSITORY, AND THAT IS DELIBERATE
-------------------------------------------------------------------------
NA-7 admits writes under exactly TWO roots: the hook's own worktree and the board.  An
earlier revision of HOOK-FALSE-POSITIVE-1 added a THIRD -- the fleet doctrine bus clone
beside this board -- and it was NARROWED OUT rather than repaired again: five review rounds
found four reproducible bypasses and EVERY one of them was in that root's machinery, while
the false-positive repairs recorded below carried none.  Read the absence as a RULE, not as
an oversight and not as a ban on publishing: PUBLISHING TO THE DOCTRINE BUS IS A GIT
OPERATION PERFORMED BY THE ORCHESTRATOR IN THAT REPOSITORY, reviewed there as a commit in
its own history, and it needs no allowance here -- the bus was in fact published to twice
while a gate with no bus allowance at all was in force.  Do not re-add a sibling-repo write
root to this gate.

Environment inputs -- ALL FIVE, none omitted (S133)
-------------------------------------------------------------------------------------
All optional, and only the test supplies overrides, so the falsifier table is
host-independent.  Four of the five NARROW what is allowed when it is unset (an absent
prompt denies every clip, an absent snapshot denies every protection mutation); the
exception is ``MLV_HOOK_DRYRUN``, which gates no allow/deny decision at all -- unset, it
only leaves the decision unprinted on stdout, and the exit code is unchanged either way.
Not one of the four narrowing inputs WIDENS an allow-list, which is the property the
removed bus root did not have and the reason its absence is now the invariant.
EVERY root/prefix comparison in this hook is made on the CANONICAL form of the path:
``norm`` collapses ``.`` and ``..`` (``canonical``, below) before ``under``/``has_seg``
compare anything, so ``<root>/../escaped.txt`` does NOT count as under ``<root>``.  Without
that collapse a rooted prefix admitted a write anywhere on the drive -- measured ALLOW
on ``f888bcfd`` and closed here, and it is KEPT because it protects the BOARD root and
NA-2's tails, not merely the root it was found on.
-------------------------------------------------------------------------------------
MLV_LANE_PROMPT             path to the lane's prompt file; NA-4 reads its
                            ``CLIP_OR_NONE:`` line, matched ``^(- )?CLIP_OR_NONE:``
                            (full cards carry the label as a bullet, fields files at
                            column 0).  Unset => any ``*.mlv`` outside the tracked
                            fixtures is DENIED.
MLV_BOARD_ROOT              default ``C:\\!Layi Wkspc\\MLV-App``.  The venue is compared
                            against THIS.
MLV_CLIP_CACHE_ROOT         default ``\\\\bachelor\\mlv-agent\\cache``.
MLV_REQUIRED_CHECKS_SNAPSHOT
                            default ``<board>\\.claude-state\\coordination\\dual-lane\\
                            receipts\\required-checks-live.jsonl``.  NA-9 reads the LAST
                            non-empty row.  Absent, unparseable, or a malformed row
                            anywhere => every protection mutation is DENIED.
MLV_HOOK_DRYRUN=1           print the decision on stdout; the exit code is unchanged.

THOSE FIVE NAMES ARE EXPANDED IN A COMMAND'S TEXT, AND NOTHING ELSE IS (S134)
----------------------------------------------------------------------------
``norm`` -- the ONE path comparison -- substitutes the five names above wherever a command
spells them ``$env:NAME``, ``${env:NAME}`` or ``%NAME%`` (NAME case-insensitively, quoted or
bare), using THE VALUE THIS PROCESS IS USING, and it does so BEFORE ``canonical`` collapses
``.``/``..``.  So every arm inherits it -- NA-2's protected tails and carve-outs, NA-7's two
roots, NA-10's gate tails -- and ``"$env:MLV_BOARD_ROOT\\..\\escaped.txt"`` cannot hide an
escape behind the variable.

WHY, MEASURED RATHER THAN ASSERTED, AND WHY THIS HALF IS KEPT.  A delete of the protected
queue ledger spelled ``"$env:MLV_BOARD_ROOT\\.claude-state\\...\\queue.json"`` was ALLOWED
while the identical LITERAL path was DENIED: the same file, the same act, spelled through
the variable THIS HOOK had already read to locate the board.  That is a real ALLOW on a
protected pen, closed by the lookup rather than by softer prose.

AN UNRESOLVABLE REFERENCE IS A DENIAL, NEVER A COLLAPSE
-------------------------------------------------------
A reference this hook cannot resolve -- ``$env:NAME``, ``${env:NAME}`` or ``%NAME%`` for a
name it does not read, or for one of the two names it reads that has no default and is
unset -- is left standing as literal text, and that text is NOT a directory.  Collapsing
``..`` against it was measured to fail OPEN: ``"$env:MLV_LANE_PROMPT/../outside.txt"``
became the bare relative ``outside.txt``, and NA-7's early return ("a relative destination
resolves inside the worktree by construction") ALLOWED it -- while the shell resolves the
same text to an absolute path outside every root.  So ``norm`` does NOT canonicalise a path
that still carries a reference after expansion, and NA-7 treats such a path as UNRESOLVABLE
and DENIES it, naming the reference.  An ordinary relative path with no reference in it
keeps exactly the behaviour it had.

THE RESIDUAL LIMIT, STATED HERE RATHER THAN PAPERED OVER.  A name this hook never read has
no value here to resolve, and inventing one would be a fabrication in the other direction;
what the hook does instead is REFUSE the path rather than guess at it.  Indirection through
a script or through an interpreter one-liner remains INVISIBLE to a text matcher, and this
hook does not pretend otherwise.  The layers behind this one are the WORKTREE BOUNDARY, the
TOOL ALLOWLIST, and the CROSS-FAMILY PR REVIEW.

WHAT "THE VALUE THIS PROCESS IS USING" MEANS.  For a name WITH a default it is the override
when the variable is set and the BUILT-IN DEFAULT when it is not -- ``hook_env_values``
answers that question once and both ``Ctx`` and ``norm`` read the answer from there.
Substituting only when the variable was PRESENT left the guard open in the DEFAULT
configuration, which is the only configuration that ships.  The two
names with NO default (``MLV_LANE_PROMPT``, ``MLV_HOOK_DRYRUN``) are still left as literal
text when unset: their effective value is empty, blanking a reference can only SHORTEN a
path out of a protected root (fail-open), and the interpreters do not even agree on the
unset case -- PowerShell expands ``$env:X`` to the empty string, cmd leaves ``%X%``
standing, so blanking would be this hook asserting which interpreter will run the command.

THE CANONICAL 0.2 ENABLE COMPOUND (O128) -- the ONE shell write the receipt paths admit::

    $ErrorActionPreference = 'Stop'; $r = '<json>'; Set-Content -LiteralPath '<receipt>'
    -Value $r -NoNewline -Encoding utf8 -ErrorAction Stop; if ((Get-Content -LiteralPath
    '<receipt>' -Raw -ErrorAction Stop) -cne $r) { throw
    'enable-receipt-write-verification-failed' }; Remove-Item -LiteralPath '<marker>'
    -ErrorAction Stop

The LEADING preference statement is load-bearing and was measured so: with a receipt path
on an unresolvable drive the provider dynamic parameters go unbound, ``-ErrorAction`` goes
unbound WITH them, and the rev-19 shape exits 0 having deleted the marker with no receipt
written.  ``$ErrorActionPreference = 'Stop'`` closes that class; the rev-19 shape (no
leading preference) is now just another non-canonical input.

ONE FIXED NOTATION FOR EVERY ``recordedUtc``, AND THE NEWEST IS THE PARSED VALUE (O141)
---------------------------------------------------------------------------------------
Every ``recordedUtc`` this hook READS or VALIDATES carries exactly ONE notation --
``YYYY-MM-DDTHH:MM:SSZ``: whole seconds, uppercase ``Z``, no fractional part, no offset,
and a real calendar date (the regex settles the notation, ``strptime`` settles the
calendar).  That is register v21's wording, and it covers every ``execution-control-*.json``
the enable act considers AND the enable literal's own ``enabledUtc`` and ``recordedUtc``.
The sixth commit accepted an OPTIONAL fractional part in the literal and never checked a
receipt's stamp at all; both are narrowed here, so a fractional literal stamp is now DENY.

WHY, MEASURED RATHER THAN ASSERTED.  The sixth commit selected the newest
execution-control receipt by comparing the raw STRINGS, and the hub reproduced the
consequence on that very commit: with ``0.6`` stamped ``2026-09-06T16:00:00Z`` and ``0.7``
stamped ``2026-09-06T16:00:00.500000Z``, ``'2026-09-06T16:00:00Z' >
'2026-09-06T16:00:00.500000Z'`` is TRUE in Python -- ``Z`` is 0x5A and ``.`` is 0x2E -- so
the STALE 0.6 was selected as newest and the one-shot enable could fire against it.  Two
arms close that class, and they are load-bearing in different ways:

  * a receipt whose ``recordedUtc`` does not conform is INVALID.  The newest is then
    UNDECIDABLE and the whole exception fails closed, naming the offending FILE -- never
    silently skipped, never treated as older.  A skipped receipt is one the board cannot
    see, and the enable would then be taken against a set nobody enumerated.
  * the newest is chosen by the PARSED value (``datetime.strptime`` of the pinned
    notation), never by a byte comparison, and an EXACT TIE between two candidates makes
    the newest UNDECIDABLE too -- DENY, naming BOTH files.  Note what this does and does
    not buy: with the notation pinned to a FIXED WIDTH, lexical order and chronological
    order coincide, so it is the VALIDATION that makes the mixed-notation case come out
    right, not the parse.  The parse is what makes "the same instant" detectable as
    EQUALITY rather than as a byte difference, and it is what the register pins.

``_kill_switch_receipts_ok`` is the ONE selection site and hands its answer down to the
literal check, so "which receipt is newest" has exactly one answer in this hook.

THE SIX GATE RECEIPTS VALIDATE AGAINST A SCHEMA TABLE (S118, register v21)
--------------------------------------------------------------------------
Up to the seventh commit, "validate" meant ``json.load`` returned something that was not
``None``.  ``{}`` is not ``None``, and the hub reproduced the consequence on the sixth
commit: FIVE EMPTY OBJECTS beside an ``execution-control-forged.json`` carrying only the
three provenance keys ALLOWED the canonical enable, exit 0.  The gate that spends the one
ratified authorization was a file-existence test wearing a schema's name.

The ONE table is plan 1.3 step 5, implemented here as ``KILL_SWITCH_RECEIPT_SCHEMAS`` plus
the chain rules.  Every gate receipt parses as JSON, is a NON-EMPTY object, and carries
``recordedUtc`` in the pinned notation above.  Value classes: ``sha256`` = 64 lowercase hex;
``sha`` = 40 lowercase hex; ``path`` = a file that EXISTS (absolute, else relative to the
board root); ``url`` = begins ``https://github.com/layibabalola/MLV-App/``; ``int`` = a
non-negative integer and a ``bool`` is NOT one; ``list`` = a JSON array.  Per receipt:

  * ``0.18-roadmap-parity.json``  queueArmResultSha256 (sha256); composedPromptPath,
    prChecksPath, prReviewPath, solVerdictPath (paths).
  * ``0.4b-required-checks.json`` headSha (sha); preContexts (list); postContexts (exactly
    the canonical five strings, containing ``Batch Compile``); snapshotRowSha256 (sha256).
  * ``0.4c-demoted.json``         headSha, mergeSha (shas); runUrl (url); solVerdictPath (path).
  * ``0.6-ratio-guard.json``      mergeSha (sha); firstReading (present and non-empty);
    solVerdictPath (path).
  * ``0.5-factory-frozen.json``   queueSha256, dryRunDiffSha256 (sha256); frozenCount (int);
    scopelessIds (list).
  * ``execution-control-<step>.json`` for <step> in {0.1, 0.35, 0.4c-i, 0.4b-i, 0.6, 0.7} --
    THE SIX CHAIN NAMES AND NO OTHER: ``reviewedHeadSha`` (sha -- the PR HEAD the second
    key reviewed BEFORE the merge); ``mergeSha`` (sha -- the post-merge commit the
    receipt's hashes were taken at, S123); ``solVerdictPath`` (path) whose TERMINAL JSON
    block carries ``verdict`` APPROVE and ``subject_sha`` equal to that ``reviewedHeadSha``
    (S120); ``hashes``, an object whose KEY SET is EXACTLY the step's
    fixed set in ``EXECUTION_CONTROL_HASH_TABLE`` and whose values are all sha256 (S120 --
    a missing or an extra key is INVALID); ``fixedSetEqualityProof``, the literal
    ``<reviewedHeadSha>=<mergeSha>:<digest>`` whose two shas EQUAL this receipt's own and
    whose digest EQUALS the sha256 of the sorted ``<sha256>  <path>`` lines recomputed from
    its own ``hashes`` (O172); 0.1 additionally ``composerStatus`` ==
    ``not-yet-created`` plus ``composedPromptPath``, ``prChecksPath`` and ``prReviewPath``
    (paths); 0.35 additionally those three paths; and from 0.35 on
    ``roadmapParityReceiptSha256`` EQUAL to the sha256 of ``0.18-roadmap-parity.json``'s
    bytes on disk, ``queueSha256`` (shape only, carried forward) and ``productLiveCount``
    exactly 15.  Any OTHER ``execution-control-*.json`` present is INVALID and fails the
    exception closed by name -- the candidate set is an ENUMERATION, not the glob the
    forged receipt walked in through.

The SELECTED control receipt is ``execution-control-0.7.json`` when a valid one exists, else
``execution-control-0.6.json``, and the parsed-newest valid chain receipt must BE the
selected one: a valid 0.6 stamped later than a valid 0.7 is a CHAIN VIOLATION (undecidable),
and the selected receipt absent is DENY.  Every refusal names the FILE and the KEY -- or the
value class -- that failed.

STRICT UNDECIDABILITY (O152).  A chain receipt that is PRESENT but invalid -- a missing or
non-conforming stamp, a missing provenance key, a malformed value -- is NEVER silently
excluded.  It makes the newest UNDECIDABLE and the act fails closed naming it.  Exclusion
was the amendment's proposal and is rejected here, because it re-opens O141's hazard in a
new shape: an invalid 0.7 beside a valid 0.6 would silently select the STALE 0.6, and the
one-shot enable would fire against a superseded receipt.  Recovery from an undecidable set
-- a malformed receipt or an exact tie -- is a NON-shrinking ``Write``-tool rewrite of the
offending receipt carrying a conforming stamp and every required key, which the receipts
carve-out allows; never a delete.  This whole arm is a PURE DECISION: it opens files for
reading and hashes one of them, and writes nothing anywhere.

THE SECOND KEY'S APPROVAL, THE FIXED HASH SET, AND THE RE-HASH (S120/O158/O159, register v22)
------------------------------------------------------------------------------------------
Up to the eighth commit a chain receipt validated with ANY non-empty ``hashes`` object and
no review at all: a receipt carrying one hashed path and no reviewer's verdict was a
complete receipt.  The gate the whole chain exists to close is "the tooling that runs the
board is the tooling the second key approved", and that reading of ``hashes`` proved neither
half.  Three arms close it:

  * S120 -- ``reviewedHeadSha`` (40 lowercase hex, the PR head the second key reviewed
    BEFORE the merge -- see S123 below) and ``solVerdictPath``, a file that exists whose
    TERMINAL JSON block -- the last ```json
    fenced block, else the last top-level JSON object in the file, which is how the
    ``sol-review-PR-TEMPLATE`` ends -- parses to an object carrying ``verdict`` ==
    ``APPROVE`` and ``subject_sha`` == that ``reviewedHeadSha``.  Absent, unreadable, a
    block that does not parse, a verdict that is not APPROVE, or an approval of a DIFFERENT
    head: INVALID, and under O152 that makes the newest undecidable.  An APPROVE of some
    other sha is the second key's approval of something else, not of this head.
  * S120 -- the ``hashes`` KEY SET is EXACTLY the step's required set, and the six sets
    live in ONE table, ``EXECUTION_CONTROL_HASH_TABLE``, written the way the plan writes
    them (BASE, then what each step adds) so the plan and the code are compared by eye in
    one place.  A missing key is a file the step did not measure; an EXTRA key is a file no
    step landed, which is how a receipt would come to vouch for a script nobody reviewed.
  * O158 -- at the enable act, AFTER the selection and the venue test, the hook RE-HASHES
    every file of the SELECTED receipt's FIXED set in the board root and compares each to
    the receipt's value.  The fixed set, never merely the keys present: the set to walk is
    the table's, and a file of that set ABSENT from the board root is a refusal naming the
    path, as is a digest that differs.  This is the one arm that reads the board's TOOLING
    rather than its receipts, and it is what makes "the receipt validated" mean "the board
    still matches it".

O159 is a hub PROCEDURE, not hook logic, and it is recorded here because the hook's strict
rule is what makes it necessary.  When the receipt that has to be rewritten is
``0.18-roadmap-parity.json`` -- whose sha256 every chain receipt from 0.35 on carries -- the
same non-shrinking ``Write`` rewrite is followed, IN THE SAME HUB STEP, by a non-shrinking
rewrite of every chain receipt from 0.35 on, re-pointing ``roadmapParityReceiptSha256`` to
the repaired file's new digest while ``queueSha256`` and ``productLiveCount`` stay as they
were: the ONE amendment to "carried forward unchanged".  The hook needs no new arm for it --
it binds every chain receipt to the 0.18 bytes on disk already -- which is exactly why a
repair that re-points all but one receipt is refused naming the one left behind, and the
suite measures both halves.

THE TWO SHAS OF A CONTROL RECEIPT (S123, register v23)
------------------------------------------------------
``reviewedHeadSha`` is the PR HEAD the second key reviewed BEFORE the merge -- the sha the
verdict's ``subject_sha`` binds to.  It is not the merge sha and can never be: a GitHub
merge lands a DIFFERENT commit, so a receipt recording the merge commit under this key
could never carry a verdict of it, and the ninth commit's one-sha reading, applied to a
real merge, would have made every post-merge control receipt INVALID.  ``mergeSha`` is the
SECOND required key -- 40 lowercase hex, the post-merge fork/master commit at which the
receipt's ``hashes`` were taken.  Absent or malformed on ANY of the six chain receipts is
INVALID, the refusal names the file and the key, and under O152 that makes the newest
undecidable.

What this hook does NOT judge, and why.  It never denies ``mergeSha == reviewedHeadSha``:
a rebase or a fast-forward lands the reviewed commit itself as the tip, and the hook does
not know the merge method -- shape only.  It makes NO git call: the equality of the step's
fixed set at the two shas (``git show <sha>:<path>`` piped to sha256, each path, at both
shas) is a HUB assertion made BEFORE the receipt is written, outside this hook.  A mismatch
there means the merge carried something the review did not see, and no control receipt is
written until sol has reviewed the merge commit itself.  The hook checks shape and the
subject binding only, never git (S123).

THE FIXED-SET EQUALITY PROOF (O172, register v25)
-------------------------------------------------
The hub's assertion that a step's fixed set hashed EQUAL at ``reviewedHeadSha`` and at
``mergeSha`` was, up to the tenth commit, recorded nowhere the hook could see: a receipt
whose two shas were well-formed validated whether or not the hub had run it.  Every one of
the six chain receipts now carries ``fixedSetEqualityProof``, the literal
``<reviewedHeadSha>=<mergeSha>:<digest>`` -- two 40-character lowercase hex shas joined by
``=``, then ``:`` and a 64-character lowercase hex sha256 -- which the hub writes only after
the assertion succeeded.  The hook validates its SHAPE, that its first sha EQUALS the
receipt's ``reviewedHeadSha`` and its second the receipt's ``mergeSha``, and that its digest
EQUALS the sha256 of the sorted ``<sha256>  <path>`` lines RECOMPUTED from the receipt's OWN
``hashes`` object -- ``_fixed_set_digest``, the ONE canonicalisation: one line per entry of
``hashes``, the digest, TWO spaces, the board-relative path (the shape ``sha256sum`` prints);
the lines sorted as plain strings (so by digest first, the order ``sort`` gives ``sha256sum``
output); joined by a single LF with NO trailing newline; UTF-8; sha256; lowercase hex.  Pure
recomputation -- no git, no file read -- so a proof pasted from another receipt, or written
before the hashes were, fails by VALUE.  Absent, malformed, a sha that does not match, or a
digest that does not match is INVALID; the refusal names the file, the key and which part
failed; and under O152 that makes the newest undecidable.  The arm runs AFTER the ``hashes``
arm, because the digest is recomputed from that object.  What the hook still does NOT judge
is unchanged from S123: the equality itself is the hub's git assertion, and what is checked
here is that the attestation is bound to THIS receipt's shas and THIS receipt's hashes.

THREE FALSE-POSITIVE CLASSES, MEASURED AND CLOSED (HOOK-FALSE-POSITIVE-1)
--------------------------------------------------------------------------
Up to this delta three arms classified an act by the TEXT AROUND it rather than by what it
named, and the board paid for all three: fourteen of twenty-seven headless editing runs
ended at ``max_turns`` after a denial, and the orchestrator was refused five times.  What
changed, and what deliberately did NOT:

  * NA-2 shell truncation.  ``shell_acts`` asked "does this text truncate anything" and the
    answer was then attributed to EVERY protected token on the line.  So
    ``cat <protected> 2>/dev/null`` was "a truncating write" of the file it only READ (the
    ``2>`` set the act; the bit bucket was the target), and a Python heredoc was too when
    its code merely contained ``if len(r) > 0``.  Truncation is now attributed ONLY when
    the redirect's or cmdlet's TARGET TOKEN itself resolves to the protected path --
    ``truncating_destinations``.  ``2>&1``, ``>&2`` and ``1>&2`` are file-descriptor dups
    and are never targets; ``>>`` and ``Add-Content`` remain appends.  DELETE and MOVE keep
    their whole-command attribution: a delete verb's operands really are its tokens.
  * Interpreter method names are still NOT verbs.  ``write_bytes(``, ``write_text(`` and
    ``open(..., 'w')`` are not shell truncation and no arm was added for them.  An
    interpreter one-liner is invisible to a text matcher BY DESIGN (the limit recorded
    under the pre-flight artifact act, unchanged); reading one as a truncation of whatever
    protected path the same command mentions proves nothing about the target and denied the
    ordinary read-here / write-there shape.
  * NA-7 destinations.  ``/dev/null``, ``NUL``, ``nul`` and ``$null`` are SINKS, not files,
    and are no longer "a write outside both roots".  THE ROOTS THEMSELVES DID NOT WIDEN:
    they are the worktree and the board, and the sibling-repository root an earlier revision
    of this delta added was narrowed back out -- see the second section of this header.
  * NA-2 generic file-tool arm.  Its refusal is about OVERWRITING ledger/pen/receipt
    content, and it fired on a ``Write`` of a path nothing existed at -- a CREATE, which
    ``_file_shrinks`` already reads as create-or-extend one branch up.  A ``Write`` to an
    ABSENT target is now allowed there, with two name classes held back at exactly their
    former behaviour: anything under a ``receipts/`` directory, and any basename that is an
    ``execution-control-*`` chain name or one of the five fixed gate receipts.  Nothing
    about DELETE, MOVE or the shrink guard changed, and ``$D/receipts/**`` still reaches
    the receipts carve-out before this arm is consulted.

THE PRE-FLIGHT ARTIFACT ACT (S125, register v25)
------------------------------------------------
Before typing the enable the hub writes the EXACT hook-stdin JSON of the final board-path
compound -- ``{"tool_name":"PowerShell","tool_input":{"command":"<the compound>"}}`` -- to
``$D\\receipts\\0.2-enable-preflight-input.json`` with the ``Write`` tool and runs the live
hook on that file under ``MLV_HOOK_DRYRUN=1`` (S104).  That content names BOTH enable paths,
and the register says any other input naming both paths is DENIED; the hub measured on the
tenth commit that the Write was ALLOWED anyway -- by the receipts carve-out, as a create
under ``$D\\receipts\\**`` -- and that the same content written to ``notes.json``, under
``fleet-runs/`` and from a worktree venue was allowed too.  S125 closes that text/code gap
BY RULE, as the SECOND dedicated act, evaluated before generic content attribution and after
the venue test: a ``Write`` (that tool name exactly) whose normalised path is exactly the
artifact's is ALLOWED only when the venue is the board root (the enable act's own test), the
target does not exist on disk, and the content parses as exactly ONE JSON object whose
``tool_name`` is ``PowerShell`` and whose ``tool_input.command`` is the canonical enable
compound the enable act would accept RIGHT NOW against the CURRENT six receipts --
``_enable_act_why``, the SAME validation (the semantic literal, the receipt schemas, the
chain selection, the O158 re-hash) on an inner context, performing nothing.  It writes
evidence only and authorizes no delete.  Any other path carrying that content
(``fleet-runs/`` included), a worktree venue, an existing target, a malformed or
multi-object content, an inner ``tool_name`` other than ``PowerShell``, or a non-canonical
or semantically invalid command is DENIED, the refusal naming the file and the reason.
GENERIC CONTENT ATTRIBUTION then applies to every other ``Write``/``Edit``/``NotebookEdit``:
content naming both enable paths, or carrying the marker under a delete verb, written to
ANY other path is DENIED -- the rule the register already stated -- while the receipts
carve-out keeps every OTHER create-or-extend under ``$D\\receipts\\**``.
``MLV_HOOK_DRYRUN=1`` prints the decision for this act exactly as for the enable act.

THE ENABLE LITERAL IS VALIDATED SEMANTICALLY, NOT FOR PRESENCE (S112, register v21)
-----------------------------------------------------------------------------------
Up to rev 21 this hook only checked that the compound's JSON literal was an object carrying
five NON-EMPTY STRINGS with ``state == "enabling"``.  Five strings of ``"x"`` passed.  So the
receipt whose PRESENCE spends the one-shot authorization (S98) -- the only durable record of
which execution-control receipt the enable was taken against -- could be written with a
receipt name and a digest that named nothing, and the gate would still open.  A receipt that
does not bind to the board it was taken on is a receipt of nothing.

The literal is now bound to the board, in this order, so a near-miss is attributable to the
ONE field that failed:

  * the five keys are present, non-empty strings (unchanged), and ``state == "enabling"``;
  * ``enabledUtc`` and ``recordedUtc`` carry the ONE pinned notation above --
    ``YYYY-MM-DDTHH:MM:SSZ``, whole seconds, uppercase ``Z``, no fraction, no offset (O141).
    The trailing ``Z`` is REQUIRED: a bare stamp is not a UTC stamp, ``+00:00`` is an
    OFFSET, which is a different notation and is refused rather than normalised, and a
    fractional part -- which the sixth commit accepted here -- is refused too, because ONE
    notation everywhere is what makes the literal and the receipts comparable at all;
  * ``executionControlReceipt`` equals the BASENAME of the NEWEST valid execution-control
    receipt -- selected by ``_kill_switch_receipts_ok`` above, the SAME selection that
    decides whether the exception is open at all, never a second implementation of it.  A
    path (``receipts/execution-control-0.7.json``) is not a basename and is refused;
  * ``executionControlSha256`` equals the LOWERCASE hex SHA-256 of THAT FILE's bytes on
    disk.  Uppercase, another file's digest and a truncated digest are all refused by exact
    string comparison -- a digest that has to be normalised before it matches was not the
    digest of that file.

The literal arm runs LAST, after the receipt state, the venue and the shape, so a lane is
never told that its literal was well-formed and only the board-rooted actor ever sees the
expected digest in a refusal line.

Stdlib only.  Deterministic: no subprocess, no clock, no network -- ``datetime`` is imported
to PARSE the pinned stamps (the literal's two and every receipt's ``recordedUtc`` -- O141),
never to read the current time, and ``hashlib`` to hash
a file already on disk and to recompute the O172 digest from a receipt's own ``hashes``.
The worktree root is derived from this file's own location
(``parents[2]``) rather than from ``git rev-parse`` in the session cwd -- it needs no
subprocess, it is the copy that actually governs the lane, and it is at least as closed as
the cwd reading.
"""

import argparse
import datetime
import hashlib
import json
import os
import posixpath
import re
import sys
import types

EXIT_ALLOW = 0
EXIT_DENY = 2

DEFAULT_BOARD_ROOT = r"C:\!Layi Wkspc\MLV-App"
DEFAULT_CLIP_CACHE_ROOT = r"\\bachelor\mlv-agent\cache"

# NOTE FOR THE NEXT READER: there is deliberately NO sibling-repository root constant here.
# An earlier revision of HOOK-FALSE-POSITIVE-1 carried one for the fleet doctrine bus and it
# was NARROWED BACK OUT, not lost: publishing to that bus is a git operation performed by the
# orchestrator IN the bus repository and reviewed there as a commit in its own history, so
# this gate needs no allowance for it.  See this file's header before adding a third root.
#
# A bit bucket is a SINK, not a file this register protects: `2>/dev/null` discards stderr
# and creates nothing.  Compared AFTER `norm`, which lowercases -- so `NUL` and `nul` are
# the one entry `nul`.
NULL_DEVICE_SINKS = frozenset(("/dev/null", "nul", "$null"))

# HOOK-FALSE-POSITIVE-1 (S134).  THE NAMES THIS HOOK ITSELF READS, and therefore the only
# names it may resolve in a command's text.  Measured defect: a delete of the protected queue
# ledger written LITERALLY was DENIED while the same delete spelled
# `"$env:MLV_BOARD_ROOT\.claude-state\...\queue.json"` was ALLOWED -- the same file, spelled
# through the very variable that told this process where the board is.  A header claiming a
# guarantee "at every value" while the code held it for one spelling is the board's recorded
# house defect, so the code is widened here and the residual limit is stated in the header.
#
# THE LIST IS THE SAME LIST `_NA3_PERSISTENT_NAMES` protects, MINUS `CLAUDE_PROJECT_DIR`,
# and the difference is load-bearing in the fail-closed direction: NA-3 protects
# `CLAUDE_PROJECT_DIR` because a PERSISTENT write of it would steer the registration, but
# this hook NEVER READS it (O126, measured absent 17/17), so it has no value here to
# substitute and inventing one would be a fabrication.  A structural test derives this tuple
# from the hook's own `env.get(...)` call sites, so a new input cannot ship unexpanded.
HOOK_READ_ENV_NAMES = (
    "MLV_BOARD_ROOT",
    "MLV_CLIP_CACHE_ROOT",
    "MLV_HOOK_DRYRUN",
    "MLV_LANE_PROMPT",
    "MLV_REQUIRED_CHECKS_SNAPSHOT",
)
_HOOK_READ_ENV_SET = frozenset(name.upper() for name in HOOK_READ_ENV_NAMES)
# Both spellings a command on this board can carry, NAME matched case-insensitively because
# Windows environment names are: PowerShell `$env:NAME` and `${env:NAME}`, cmd `%NAME%`.
# Surrounding quotes are stripped by `norm` before this runs, so the quoted and bare forms
# reach it identically.
_ENV_REF_RX = re.compile(
    r"\$env:(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\$\{env:(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|%(?P<cmd>[A-Za-z_][A-Za-z0-9_]*)%",
    re.I,
)

SHELL_TOOLS = ("Bash", "PowerShell")
FILE_TOOLS = ("Write", "Edit", "NotebookEdit")
MATCHED_TOOLS = SHELL_TOOLS + FILE_TOOLS

# Relative tails, normalised.  Every containment test below is segment-wise, so the same
# marker matches an absolute path, a worktree-relative path and a board-relative one.
DUAL_TAIL = ".claude-state/coordination/dual-lane"
FLEET_TAIL = ".claude-state/fleet-runs"
NA2_PROTECTED_TAILS = (
    ".claude-state/closeout",
    FLEET_TAIL,
    ".claude-state/coordination",
    ".closeout-evidence",
)
NA2_PROTECTED_FILE_TAIL = ".claude/analysis_log.md"
KILL_SWITCH_LEAF = "workstream-loop-disabled"
KILL_SWITCH_MARKER_NAME = "WORKSTREAM-LOOP-DISABLED"

CANONICAL_04B_CONTEXTS = frozenset(
    (
        "Repo Hygiene Python (windows-latest)",
        "Repo Hygiene Python (ubuntu-latest)",
        "Windows GUI Pilot",
        "Windows Product Oracles",
        "Batch Compile",
    )
)
CANONICAL_04B_APP_ID = 15368

# S118 -- THE RECEIPT SCHEMA TABLE, and it is the ONE table (plan 1.3 step 5).
#
# Up to the seventh commit "validate" meant `json.load` returned something that was not
# `None`.  `{}` is not `None`, so FIVE EMPTY OBJECTS validated -- and the hub reproduced
# exactly that on the sixth commit: five `{}` receipts beside an `execution-control-
# forged.json` carrying only the three provenance keys ALLOWED the one-shot enable.  The
# gate that spends the single ratified authorization was, in effect, a file-existence test.
#
# The VALUE CLASSES the plan names.  Each is a predicate over a JSON value, and each refusal
# below names the FILE and the KEY (or the class) that failed, so a near-miss receipt is
# attributable to one field:
#
#   sha256    exactly 64 LOWERCASE hex.  Uppercase is refused rather than folded, and 63
#             characters is refused rather than prefix-matched -- the same exactness the
#             enable literal's digest already carries (S112).
#   sha       exactly 40 LOWERCASE hex -- a git object name, not a digest.
#   path      a file that EXISTS, read as absolute first and then relative to the board
#             root, because the plan writes these fields relative to `$R`.  A directory is
#             not a file: `os.path.isfile` is the test, so a receipt naming its own parent
#             directory does not pass.
#   url       begins `https://github.com/layibabalola/MLV-App/`.  A PREFIX, because the
#             plan pins the repository and not the route -- and the board carries two
#             remotes, so a run URL on the UPSTREAM host is exactly the value this refuses.
#   int       a NON-NEGATIVE integer, and `bool` is NOT one: `True == 1` in Python, so a
#             `frozenCount` of `true` would otherwise pass an `isinstance(v, int)` test.
#   list      a JSON array.
#   nonempty  present, not `None`, and -- for a string, array or object -- not empty.  A
#             number or a boolean passes on presence; the plan asks only that
#             `firstReading` be there and carry something.
SHA256_HEX_RX = re.compile(r"\A[0-9a-f]{64}\Z")
SHA_HEX_RX = re.compile(r"\A[0-9a-f]{40}\Z")
RECEIPT_URL_PREFIX = "https://github.com/layibabalola/MLV-App/"
ROADMAP_PARITY_RECEIPT = "0.18-roadmap-parity.json"
# `postContexts` is the canonical five of 0.4b, and `Batch Compile` is the context the whole
# step exists to promote -- a four-entry list is the shape a silent removal leaves behind.
CANONICAL_04B_CONTEXT_COUNT = 5
CANONICAL_04B_PROMOTED_CONTEXT = "Batch Compile"

# The five FIXED gate receipts, each with the required keys of its producing step.  The
# order is the register's, and it is also the order the arms fire in, so a set with two
# faults is attributed to the first one the plan lists.  `recordedUtc` is not in any row
# because EVERY gate receipt carries it, checked once for all six.
KILL_SWITCH_RECEIPT_SCHEMAS = (
    (
        ROADMAP_PARITY_RECEIPT,
        (
            ("queueArmResultSha256", "sha256"),
            ("composedPromptPath", "path"),
            ("prChecksPath", "path"),
            ("prReviewPath", "path"),
            ("solVerdictPath", "path"),
        ),
    ),
    (
        "0.4b-required-checks.json",
        (
            ("headSha", "sha"),
            ("preContexts", "list"),
            ("postContexts", "contexts"),
            ("snapshotRowSha256", "sha256"),
        ),
    ),
    (
        "0.4c-demoted.json",
        (
            ("headSha", "sha"),
            ("mergeSha", "sha"),
            ("runUrl", "url"),
            ("solVerdictPath", "path"),
        ),
    ),
    (
        "0.6-ratio-guard.json",
        (
            ("mergeSha", "sha"),
            ("firstReading", "nonempty"),
            ("solVerdictPath", "path"),
        ),
    ),
    (
        "0.5-factory-frozen.json",
        (
            ("queueSha256", "sha256"),
            ("frozenCount", "int"),
            ("dryRunDiffSha256", "sha256"),
            ("scopelessIds", "list"),
        ),
    ),
)
KILL_SWITCH_RECEIPTS = tuple(name for name, _ in KILL_SWITCH_RECEIPT_SCHEMAS)
RECORDED_UTC_KEY = "recordedUtc"

# THE EXECUTION-CONTROL CHAIN IS SIX NAMES AND NO OTHER (S118).  The candidate set was
# `execution-control-*.json`, a GLOB, so any file matching it joined the selection -- and
# the hub's reproduction used precisely that: `execution-control-forged.json`, a name no
# plan step writes, carrying only the provenance keys.  The set is now an enumeration, and
# a file matching the glob but not the enumeration does not merely get skipped: it FAILS
# THE EXCEPTION CLOSED, because a receipt nobody can attribute to a step is a receipt the
# board cannot account for.
EXECUTION_CONTROL_PREFIX = "execution-control-"
EXECUTION_CONTROL_SUFFIX = ".json"
EXECUTION_CONTROL_STEPS = ("0.1", "0.35", "0.4c-i", "0.4b-i", "0.6", "0.7")
EXECUTION_CONTROL_CHAIN = tuple(
    EXECUTION_CONTROL_PREFIX + step + EXECUTION_CONTROL_SUFFIX
    for step in EXECUTION_CONTROL_STEPS
)
# "from 0.35 on": every chain step EXCEPT 0.1, which runs before 0.18 exists and therefore
# cannot carry a digest of it.
EXECUTION_CONTROL_PROVENANCE_EXEMPT = (
    EXECUTION_CONTROL_PREFIX + EXECUTION_CONTROL_STEPS[0] + EXECUTION_CONTROL_SUFFIX,
)
# The SELECTED receipt is a NAME, not "whichever is newest": 0.7 when a valid one exists,
# else 0.6.  The parsed-newest valid chain receipt must BE that one -- a valid 0.6 stamped
# later than a valid 0.7 is a chain violation, which is undecidable rather than a new
# answer to "which is newest".
CONTROL_SELECTED_PREFERRED = EXECUTION_CONTROL_PREFIX + "0.7" + EXECUTION_CONTROL_SUFFIX
CONTROL_SELECTED_FALLBACK = EXECUTION_CONTROL_PREFIX + "0.6" + EXECUTION_CONTROL_SUFFIX
CONTROL_HASHES_KEY = "hashes"

# S120 -- EVERY CONTROL RECEIPT CARRIES THE SECOND KEY'S APPROVAL OF THIS HEAD.
# `reviewedHeadSha` is the PR HEAD sol reviewed for this step BEFORE the merge;
# `solVerdictPath` names that review, whose TERMINAL JSON block -- the
# `sol-review-PR-TEMPLATE` ends with exactly one -- must carry `verdict` APPROVE and
# `subject_sha` equal to that head.  The verdict literal and the key names are the
# template's, compared exactly.
CONTROL_REVIEWED_HEAD_KEY = "reviewedHeadSha"
# S123 -- AND THE SECOND SHA.  A GitHub merge lands a DIFFERENT commit from the reviewed
# head, so `reviewedHeadSha` can never be the merge sha; `mergeSha` is the post-merge
# fork/master commit the receipt's `hashes` were taken at.  Both are REQUIRED, 40 lowercase
# hex.  Their EQUALITY is neither required nor denied -- a rebase lands the reviewed commit
# itself as the tip and the hook does not know the merge method -- and the fixed set hashing
# equal at both shas is the HUB's assertion before the receipt is written, never a git call
# from here: shape and the subject binding only.
CONTROL_MERGE_SHA_KEY = "mergeSha"
# O172 -- THE FIXED-SET EQUALITY PROOF.  The hub asserts, with git and BEFORE the receipt is
# written, that the step's fixed set hashes equal at both shas; the receipt records that it
# did as `<reviewedHeadSha>=<mergeSha>:<digest>`, and the hook binds the record to THIS
# receipt: both shas equal the receipt's own, and the digest equals `_fixed_set_digest` of
# the receipt's own `hashes` -- so a proof cannot be pasted from another receipt.
CONTROL_EQUALITY_PROOF_KEY = "fixedSetEqualityProof"
_EQUALITY_PROOF_RX = re.compile(
    r"\A(?P<reviewed>[0-9a-f]{40})=(?P<merge>[0-9a-f]{40}):(?P<digest>[0-9a-f]{64})\Z"
)
CONTROL_VERDICT_PATH_KEY = "solVerdictPath"
CONTROL_VERDICT_KEY = "verdict"
CONTROL_VERDICT_APPROVE = "APPROVE"
CONTROL_VERDICT_SUBJECT_KEY = "subject_sha"
# The terminal block: the LAST ```json fence when the file carries any (a fence is the
# template's shape), else the LAST top-level JSON object -- a `{` at column 0 that decodes
# as an object and is not inside an object already decoded (a plain `.json` verdict).
_VERDICT_FENCE_RX = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
_VERDICT_OBJECT_HEAD_RX = re.compile(r"^\{", re.M)

# S120/O158 -- THE ONE TABLE OF FIXED HASH SETS (plan 1.3 step 5), written the way the plan
# writes it so the two are compared by eye in one place: BASE, then what each step ADDS to
# the previous step's set.  0.7 adds nothing -- it carries 0.6's set at a later head.  The
# `hashes` KEY SET of a chain receipt is EXACTLY its step's set (a missing key is a file the
# step did not measure, an extra key a file no step landed), and at the enable act 0.2
# re-hashes the SELECTED receipt's FIXED set in the board root -- never merely the keys
# present (O158).  Paths are the plan's, forward-slashed and board-relative.
CONTROL_HASHES_BASE = (
    "tools/hooks/mlv-never-authorized.py",
    "tools/repo_hygiene/test_mlv_never_authorized.py",
    "tools/hooks/test_registration_path_local.py",
    "tools/coordination/Invoke-Lane.ps1",
    "tools/coordination/Invoke-Workstream.ps1",
    "tools/coordination/Invoke-WorkstreamLoop.ps1",
)
EXECUTION_CONTROL_HASH_TABLE = (
    ("0.1", ()),
    ("0.35", ("tools/coordination/Compose-LanePrompt.ps1",)),
    ("0.4c-i", ("tools/coordination/demote-factory-bridge.ps1",)),
    ("0.4b-i", ("tools/coordination/set-required-checks.ps1",)),
    (
        "0.6",
        (
            "tools/coordination/Test-ProductRatioGuard.ps1",
            "tools/coordination/freeze-factory-cards.py",
        ),
    ),
    ("0.7", ()),
)


def _required_hash_sets():
    """-> {chain receipt basename: frozenset of board-relative paths}, from the table above."""
    sets = {}
    running = tuple(CONTROL_HASHES_BASE)
    for step, added in EXECUTION_CONTROL_HASH_TABLE:
        running = running + tuple(added)
        sets[EXECUTION_CONTROL_PREFIX + step + EXECUTION_CONTROL_SUFFIX] = frozenset(running)
    return sets


EXECUTION_CONTROL_REQUIRED_HASHES = _required_hash_sets()

# S120 -- THE PER-STEP KEYS.  0.1 runs before the composer exists, so it records that fact
# (`composerStatus` == `not-yet-created`) beside the three evidence paths; 0.35 lands the
# composer and records the three paths; the provenance keys (`PROVENANCE_KEYS`, from 0.35
# on) are unchanged from the eighth commit.
CONTROL_COMPOSER_STATUS_KEY = "composerStatus"
CONTROL_COMPOSER_STATUS_VALUE = "not-yet-created"
CONTROL_COMPOSER_PATH_KEYS = ("composedPromptPath", "prChecksPath", "prReviewPath")
EXECUTION_CONTROL_COMPOSER_STATUS_NAMES = (
    EXECUTION_CONTROL_PREFIX + "0.1" + EXECUTION_CONTROL_SUFFIX,
)
EXECUTION_CONTROL_COMPOSER_PATH_NAMES = (
    EXECUTION_CONTROL_PREFIX + "0.1" + EXECUTION_CONTROL_SUFFIX,
    EXECUTION_CONTROL_PREFIX + "0.35" + EXECUTION_CONTROL_SUFFIX,
)
# S98: the 0.2 enable is ONE-SHOT.  0.2 writes this receipt in the SAME guarded action as
# the delete, so its PRESENCE is the proof the one authorization was already spent.
KILL_SWITCH_ENABLE_RECEIPT = "0.2-loop-enabled.json"
# S125: the PRE-FLIGHT ARTIFACT, the SECOND dedicated act.  Before typing the enable the hub
# writes the EXACT hook-stdin JSON of the final board-path compound to this receipt path
# with the `Write` tool and runs the live hook on it under MLV_HOOK_DRYRUN=1 (S104).  That
# Write is judged as ONE act -- `PREFLIGHT_TOOL` exactly, its content one JSON object whose
# `tool_name` is `PREFLIGHT_INNER_TOOL` -- before generic content attribution, and it is the
# only file-tool input that may carry content naming both enable paths.
KILL_SWITCH_PREFLIGHT_ARTIFACT = "0.2-enable-preflight-input.json"
PREFLIGHT_TOOL = "Write"
PREFLIGHT_INNER_TOOL = "PowerShell"
PROVENANCE_KEYS = ("roadmapParityReceiptSha256", "queueSha256", "productLiveCount")
PROVENANCE_PARITY_KEY = "roadmapParityReceiptSha256"
PROVENANCE_QUEUE_KEY = "queueSha256"
PROVENANCE_COUNT_KEY = "productLiveCount"
PROVENANCE_PRODUCT_LIVE_COUNT = 15

# S99/O118: the 0.2 enable is ONE DEDICATED ACT, not a verb plus a path.  The register's
# canonical compound and the keys its JSON literal must carry.
ENABLE_LITERAL_VARIABLE = "r"
ENABLE_LITERAL_STATE = "enabling"
ENABLE_LITERAL_KEYS = (
    "state",
    "enabledUtc",
    "executionControlReceipt",
    "executionControlSha256",
    "recordedUtc",
)
# S112/O141: the two stamps carry the ONE pinned notation, in the order the literal is
# read, so a row varying one is attributable to that one.  The regex pins the NOTATION --
# whole seconds, uppercase `Z`, no fractional part, no offset, nothing before or after --
# and `strptime` then proves the CALENDAR, so `2026-02-30T00:00:00Z` is refused even though
# its shape is right.  THE SAME notation governs every `recordedUtc` this hook reads off a
# receipt: the sixth commit accepted an optional fraction here and compared receipt stamps
# as raw bytes, and the two together let a STALE receipt be selected as newest (O141).  No
# current time is read anywhere -- the register asks whether the stamp parses, not when it
# is.
ENABLE_LITERAL_UTC_KEYS = ("enabledUtc", "recordedUtc")
PINNED_UTC_NOTATION = "YYYY-MM-DDTHH:MM:SSZ"
_PINNED_UTC_RX = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_PINNED_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ENABLE_CONTROL_NAME_KEY = "executionControlReceipt"
ENABLE_CONTROL_SHA_KEY = "executionControlSha256"
# S101: the canonical compound is FAIL-CLOSED.  A `Set-Content` failure is NON-terminating
# by default, so the rev-18 shape would let `Remove-Item` disarm the kill switch with the
# receipt absent -- the exact state the enable exists to make impossible.  The register now
# pins `-ErrorAction Stop` on BOTH acts and a read-back `throw` between them, and the hook
# matches ONLY that shape; the rev-18 shape is just another non-canonical input.
ENABLE_THROW_MESSAGE = "enable-receipt-write-verification-failed"

# O128: the canonical compound OPENS with the preference statement.  The value is the
# PowerShell ``ActionPreference`` enum member, which PowerShell resolves case-insensitively,
# so it is compared that way -- exactly like the two ``-ErrorAction Stop`` arms in the same
# compound.  (The ``throw`` literal above stays CASE-SENSITIVE: it is not an enum, it is the
# string a later reader greps for when asked whether an enable halted at the write.)
ENABLE_PREFERENCE_VARIABLE = "erroractionpreference"
ENABLE_PREFERENCE_VALUE = "stop"

# O124: NA-2 exception (iv) -- the four ratified manifest surfaces, relative to $D.
MANIFEST_PLAN_GLOB_PREFIX = "definitive-fix-plan-"
MANIFEST_PLAN_GLOB_SUFFIX = ".md"
MANIFEST_EXACT_LEAVES = ("never-authorized.json", "start-editinglane.ps1")
MANIFEST_PROMPTS_PREFIX = "prompts/v2/"

# NA-10 (O129, hub extension): a lane editing its OWN gate.  Path TAILS, matched
# segment-wise against the END of the path, UNDER ANY ROOT -- the rule is about which file
# is being written, not about which tree it sits in, because every lane worktree carries its
# own copy of all three and each one governs that lane's next tool call.  (NA-4's owner-
# consented footage table lives IN the hook script, so the third tail already guards it.  The
# FOURTH tail is that table's content verifier (NA4-OWNER-CONSENTED-FOOTAGE-1 round 3, E):
# every tracked, id-addressed footage consumer must call it before opening a clip, so a lane
# that rewrites it rewrites the only content check those consumers have.)
NA10_GUARDED_TAILS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    "tools/hooks/mlv-never-authorized.py",
    "tools/gates/verify_consented_footage.py",
)


class Deny(Exception):
    """A rule fired.  ``rule`` is the register row id, ``reason`` the one-line why."""

    def __init__(self, rule, reason):
        super().__init__("%s: %s" % (rule, reason))
        self.rule = rule
        self.reason = reason


# --------------------------------------------------------------------------- paths


_DRIVE_RX = re.compile(r"^([a-z]:)(/.*)?$")


def canonical(text):
    """Collapse ``.`` and ``..`` segments so ONE path has ONE comparable form (S133).

    THE DEFECT THIS CLOSES, measured by sol on PR #104 against
    ``f888bcfd``: ``norm`` folded case and separators but never collapsed ``..``, so
    ``<root>/../escaped.txt`` still carried the root as a literal PREFIX and ``under()``
    returned True for a path that resolves OUTSIDE it.  A ``Write`` there was ALLOWED.  The
    measurement was taken on a root this delta has since removed, but the collapse is KEPT
    and is not about that root: it protects the BOARD root and NA-2's protected tails, which
    the same escape reached.  Every prefix test in this hook -- NA-7's two
    roots, NA-2's protected tails and carve-outs, NA-10's gate tails -- is a comparison of
    STRINGS, so the collapse has to happen before any of them, and the one place that is
    true of all of them is ``norm`` itself, which is the hook's ONE path comparison.

    ``posixpath.normpath``, not ``os.path.normpath``, and the difference is load-bearing:
    this suite runs on windows-latest AND ubuntu-latest, ``ntpath.normpath`` returns
    backslashes while ``posixpath.normpath`` does not, and ``ntpath`` alone knows ``c:`` is
    a drive -- so ``os.path.normpath`` would give ONE input TWO canonical forms depending on
    the runner.  The drive is split off here instead and the remainder is normalised as an
    absolute POSIX path, which makes the answer identical on both legs AND clamps at the
    root: ``c:/../escaped.txt`` is ``c:/escaped.txt``, never the drive-less ``escaped.txt``
    that ``posixpath`` alone would return and that ``is_absolute`` would then wave past
    NA-7's early return.  The filesystem is still not consulted and links are still not
    followed -- this is string algebra, and a ``..`` through a symlink is a limit recorded
    here rather than a stat call in the latency budget.
    """
    if not text or "." not in text:
        return text  # no dot, no `.`/`..` segment -- nothing to collapse
    drive = ""
    match = _DRIVE_RX.match(text)
    if match:
        drive = match.group(1)
        text = match.group(2) or "/"
    return drive + posixpath.normpath(text)


def hook_env_values():
    """THE ONE RESOLUTION POINT: every read name -> THE VALUE THIS HOOK IS ACTUALLY USING.

    "IS THE VARIABLE SET?" IS THE WRONG QUESTION, AND ASKING IT WAS A MEASURED HOLE.
    An earlier revision substituted a name only when it was actually PRESENT in this
    process's environment.  With a steering name UNSET -- the ambient state on this machine,
    since only the falsifier suite ever exports one -- an act spelled ``"$env:NAME\\..."``
    returned ALLOW while the very same act written LITERALLY was DENIED.  The hook had not
    failed to KNOW the root in that configuration; ``Ctx`` resolved it to the BUILT-IN
    DEFAULT two lines later.  The right question is "what value is this process using?", and
    the default configuration is the only one that ships.

    So the answer for a name is the OVERRIDE when the variable carries one and the hook's
    OWN BUILT-IN DEFAULT when it does not, exactly as ``Ctx`` resolves it -- and ``Ctx`` now
    reads its roots from HERE, so the two readings cannot drift apart again.  The ``or``
    (never ``is None``) is deliberate and mirrors ``Ctx``'s: an EMPTY override is not an
    override, and both sides must agree on that or the expansion would substitute ``""``
    where the rules use the default.

    THE TWO NAMES WITH NO DEFAULT KEEP THE ROUND-3 READING, and that is not an oversight.
    ``MLV_LANE_PROMPT`` is mapped to ``None`` when unset rather than to its effective ``""``,
    and ``MLV_HOOK_DRYRUN`` likewise: their unset values are not PATHS, and blanking a
    reference is the one substitution that can only ever SHORTEN a path -- moving it OUT of a
    protected root, i.e. failing OPEN.  A reference left standing as literal text fabricates
    nothing and can only fail closed.  ``CLAUDE_PROJECT_DIR`` is absent from this table
    entirely for the reason stated in the header: this hook never reads it, so it has no
    value here to substitute and inventing one would be the fabrication S134 refuses.
    """
    env = os.environ
    board_root_raw = env.get("MLV_BOARD_ROOT") or DEFAULT_BOARD_ROOT
    return {
        "MLV_BOARD_ROOT": board_root_raw,
        "MLV_CLIP_CACHE_ROOT": env.get("MLV_CLIP_CACHE_ROOT") or DEFAULT_CLIP_CACHE_ROOT,
        # Its default is DERIVED from the board root resolved just above, so an override of
        # the board alone moves this too -- the same chain `Ctx` builds.
        "MLV_REQUIRED_CHECKS_SNAPSHOT": env.get("MLV_REQUIRED_CHECKS_SNAPSHOT")
        or os.path.join(
            board_root_raw,
            ".claude-state",
            "coordination",
            "dual-lane",
            "receipts",
            "required-checks-live.jsonl",
        ),
        # No default: see the docstring's last paragraph.  `None` means LEAVE IT LITERAL.
        "MLV_LANE_PROMPT": env.get("MLV_LANE_PROMPT") or None,
        "MLV_HOOK_DRYRUN": os.environ.get("MLV_HOOK_DRYRUN") or None,
    }


def expand_hook_env(text):
    """Substitute the environment variables THIS HOOK READS, in either spelling (S134).

    THE DEFECT THIS CLOSES, measured on PR #104: a delete of the protected queue ledger
    written ``Remove-Item -LiteralPath '<board>/.claude-state/.../queue.json'`` was DENIED
    while ``Remove-Item -LiteralPath "$env:MLV_BOARD_ROOT\\.claude-state\\...\\queue.json"``
    was ALLOWED.  Same file, same act, spelled through the very variable that told THIS
    PROCESS where the board is -- so the value was not unknown to the hook, it was simply
    never looked up.

    WHAT IS SUBSTITUTED, AND WHY THAT IS THE HONEST BOUNDARY.  Only the names in
    ``HOOK_READ_ENV_NAMES``, and at the value ``hook_env_values`` says this process is
    USING -- the override when the variable is set, the built-in default when it is not.
    Two halves:

      * a name this hook does not read has no value here that is more authoritative than
        the shell's, and guessing one would deny (or admit) a path the hook never resolved.
        Such a reference is left STANDING, and ``_na7_check_path`` then refuses the path as
        UNRESOLVABLE rather than letting ``canonical`` collapse it into a bare relative name;
      * a name this hook DOES read is resolved in BOTH configurations, because an unset
        steering variable does not leave the hook ignorant of the root -- it leaves the hook
        using its DEFAULT.  Substituting only when the variable was set meant the guard held
        only on the machine the falsifier suite builds and not on the one that ships.  Only a
        name with NO default (``MLV_LANE_PROMPT``) is still left standing when unset, for the
        fail-closed reason in ``hook_env_values`` -- and standing now means DENIED at NA-7.

    ONE PASS, never re-scanned: a value that itself contains ``$env:...`` is substituted
    once and then treated as text, so no input can drive this into a loop.

    Called from ``norm`` -- the hook's ONE path comparison -- and BEFORE ``canonical``, so
    ``"$env:MLV_BOARD_ROOT\\..\\escaped.txt"`` is expanded and THEN collapsed, and the
    ``..`` cannot hide behind the variable.
    """
    if "$" not in text and "%" not in text:
        return text  # neither spelling can be present -- the common case, no scan

    values = hook_env_values()  # resolved once per scan, never inside the substitution loop

    def _one(match):
        name = match.group("bare") or match.group("braced") or match.group("cmd")
        if name.upper() not in _HOOK_READ_ENV_SET:
            return match.group(0)
        value = values.get(name.upper())
        if value is None:
            return match.group(0)
        return value

    return _ENV_REF_RX.sub(_one, text)


def _unresolved_env_ref(text):
    # Returns the FIRST environment reference still standing in `text`, else None.
    #
    # Called on a path `expand_hook_env` has already been over, so anything this finds is a
    # reference the hook CANNOT resolve: a name it does not read, or one of the two names it
    # does read that has no default and is unset.  Such text is not a directory, and both
    # `norm` (which declines to collapse `..` against it) and NA-7 (which denies it) key on
    # this ONE answer rather than each re-deriving it.
    if "$" not in text and "%" not in text:
        return None
    match = _ENV_REF_RX.search(text)
    return match.group(0) if match else None


def norm(path):
    """Normalise for comparison: backslashes to slashes, lowercased, no trailing slash.

    The environment variables THIS HOOK READS are EXPANDED first (``expand_hook_env``
    above), then ``.`` and ``..`` are COLLAPSED (``canonical``), so a path that walks out of
    a root cannot keep that root as a prefix -- whether the root arrived literally or
    through ``$env:``/``%``.  Both live here, in the hook's ONE path comparison, so every
    arm inherits them: NA-2's protected tails and carve-outs, NA-7's two roots, and NA-10's
    gate tails.

    THE COLLAPSE IS SKIPPED WHEN A REFERENCE IS STILL STANDING, and that is the round-5
    repair.  A reference this hook cannot resolve is TEXT, not a directory, so collapsing a
    following ``..`` against it invents a parent that does not exist -- measured:
    ``"$env:MLV_LANE_PROMPT/../outside.txt"`` became the bare relative ``outside.txt`` and
    NA-7's early return ALLOWED it, while the shell resolves the same text to an absolute
    path outside every root.  The path is left uncollapsed here and DENIED there
    (``_na7_check_path``); a path with no reference in it is collapsed exactly as before.

    Links are NOT followed and the filesystem is NOT consulted -- NA-4 requires the
    comparison to be made without following links, and a hook that stats every token is a
    hook that misses its latency budget.
    """
    text = expand_hook_env(str(path).strip().strip('"').strip("'")).replace("\\", "/")
    unc = text.startswith("//")
    text = re.sub(r"/{2,}", "/", text)
    if unc:
        text = "/" + text
    text = text.lower()
    if _unresolved_env_ref(text) is None:
        text = canonical(text)
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def after_seg(path_norm, seg_norm):
    """Remainder of ``path_norm`` after a whole-segment run of ``seg_norm``.

    Returns ``None`` when the run is absent, ``""`` when the path IS the run.
    """
    if path_norm == seg_norm:
        return ""
    if path_norm.startswith(seg_norm + "/"):
        return path_norm[len(seg_norm) + 1 :]
    key = "/" + seg_norm
    index = path_norm.find(key)
    while index != -1:
        end = index + len(key)
        if end == len(path_norm):
            return ""
        if path_norm[end] == "/":
            return path_norm[end + 1 :]
        index = path_norm.find(key, index + 1)
    return None


def has_seg(path_norm, seg_norm):
    return after_seg(path_norm, seg_norm) is not None


def under(path_norm, root_norm):
    """True when ``path_norm`` is ``root_norm`` or lives beneath it."""
    if not root_norm:
        return False
    return path_norm == root_norm or path_norm.startswith(root_norm + "/")


def is_absolute(path_norm):
    # A Windows drive-letter path (c:/...) or a UNC/POSIX path rooted at / (// is UNC,
    # a single / is a POSIX absolute path -- both start with /, so one alternative
    # covers both). The prior form matched ONLY c:/ or // and silently treated a real
    # POSIX absolute path (e.g. /tmp/x) as "not absolute", which made
    # _na7_check_path's early return ("a relative destination resolves inside the
    # worktree by construction") fire on genuinely absolute paths outside the
    # worktree/board roots -- NA-7 never denied them. Found by hosted CI on
    # ubuntu-latest (r1/r2 falsifiers), invisible on the Windows-only local suite.
    return bool(re.match(r"^(?:[a-z]:/|/)", path_norm))


# ------------------------------------------------------------------------- tokens

_TOKEN_RX = re.compile(r"\"([^\"]*)\"|'([^']*)'|([^\s'\"=,;|&()\[\]<>]+)")


def tokens(text):
    """Quoted strings and bare runs, in order.  Quotes are stripped, nothing is executed."""
    found = []
    for match in _TOKEN_RX.finditer(text or ""):
        value = match.group(1)
        if value is None:
            value = match.group(2)
        if value is None:
            value = match.group(3)
        if value:
            found.append(value)
    return found


def json_blobs(text):
    """Balanced ``{...}`` runs, string-aware.  Used to recover an inline API body."""
    blobs = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                blobs.append(text[start : index + 1])
                start = -1
    return blobs


# ------------------------------------------------------------------------ context


class Ctx(object):
    """Everything a rule needs, resolved once."""

    def __init__(self, tool, tool_input, project_dir):
        self.tool = tool
        self.tool_input = tool_input
        # ROUND 5: resolved by `hook_env_values`, the SAME call `expand_hook_env`
        # substitutes from -- so "the value this hook uses" is one answer, not two that can
        # drift.  Round 4's hole was exactly that drift: `Ctx` fell back to the default while
        # the expansion left the reference literal, so the guard held only when the variable
        # happened to be set.
        env = hook_env_values()
        self.board_root = norm(env["MLV_BOARD_ROOT"])
        self.board_root_raw = env["MLV_BOARD_ROOT"]
        self.clip_cache_root = norm(env["MLV_CLIP_CACHE_ROOT"])
        self.worktree_root = norm(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        self.worktree_root_raw = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.dual_dir_raw = os.path.join(
            self.board_root_raw, ".claude-state", "coordination", "dual-lane"
        )
        self.receipts_dir_raw = os.path.join(self.dual_dir_raw, "receipts")
        self.snapshot_raw = env["MLV_REQUIRED_CHECKS_SNAPSHOT"]
        self.lane_prompt = env["MLV_LANE_PROMPT"] or ""
        # There is deliberately NO sibling-repository root here.  NA-7's roots are the
        # worktree and the board; see this file's header for why the fleet doctrine bus root
        # was narrowed back out rather than repaired.
        # O126, THE VENUE, and it rides on ARGV.  `--project-dir` is substituted by Claude
        # Code from `${CLAUDE_PROJECT_DIR}` in the REGISTERED command, so its value is
        # fixed by `.claude/settings.json`: a tool call cannot set an argument, and NA-10
        # denies a lane editing the registration that carries it.  The ENVIRONMENT variable
        # of that name is NOT consulted here and must not be -- it was measured absent from
        # real hook processes (null in 17/17), so the rev-19 venue test never fired at all.
        #
        # `norm()` is the hook's one path comparison -- the same function that decides every
        # other containment here -- so `C:\!Layi Wkspc\MLV-App`, `C:/!Layi Wkspc/MLV-App`
        # and a trailing-slash form are ONE venue, and `C:\mlvtmp\plan-definitive-fix-v7` is
        # not.  MISSING or EMPTY is not "unknown, assume the hub": it means every
        # venue-keyed exception is closed (O124 exception (iv), S105's enable act, NA-10).
        self.project_dir_raw = project_dir or ""
        self.project_dir = norm(self.project_dir_raw)
        self.at_board_venue = bool(self.project_dir) and self.project_dir == self.board_root

        self.command = ""
        self.path = ""
        self.path_norm = ""
        self.old_text = None
        self.new_text = None

        if tool in SHELL_TOOLS:
            command = tool_input.get("command")
            if command is not None and not isinstance(command, str):
                raise Deny("hook-error", "non-string command for %s" % tool)
            self.command = command or ""
        elif tool == "Write":
            self.path = _first_str(tool_input, ("file_path", "path"))
            self.new_text = _opt_str(tool_input, "content")
        elif tool == "Edit":
            self.path = _first_str(tool_input, ("file_path", "path"))
            self.old_text = _opt_str(tool_input, "old_string")
            self.new_text = _opt_str(tool_input, "new_string")
        elif tool == "NotebookEdit":
            self.path = _first_str(tool_input, ("notebook_path", "file_path", "path"))
            self.new_text = _opt_str(tool_input, "new_source")
        self.path_norm = norm(self.path) if self.path else ""

    @property
    def subject(self):
        """The whole text a pattern rule may look at, for this tool."""
        if self.tool in SHELL_TOOLS:
            return self.command
        parts = [self.path or ""]
        if self.old_text:
            parts.append(self.old_text)
        if self.new_text:
            parts.append(self.new_text)
        return "\n".join(parts)

    def receipt(self, name):
        return os.path.join(self.receipts_dir_raw, name)


def _first_str(tool_input, keys):
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _opt_str(tool_input, key):
    value = tool_input.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise Deny("hook-error", "non-string %s" % key)
    return value


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


# ------------------------------------------------------------------------ verbs

_DELETE_RX = re.compile(
    r"(?:^|[\s;&|`(])(?:rm|del|rd|rmdir)\b"
    r"|\bremove-item\b"
    r"|\bgit\s+rm\b"
    r"|\bos\.(?:remove|unlink)\s*\("
    r"|\bshutil\.rmtree\s*\("
    r"|\.unlink\s*\(",
    re.I,
)
_MOVE_RX = re.compile(
    r"(?:^|[\s;&|`(])(?:mv|move)\b"
    r"|\bmove-item\b"
    r"|\brename-item\b"
    r"|\bgit\s+mv\b"
    r"|\bos\.rename\s*\("
    r"|\bshutil\.move\s*\(",
    re.I,
)
# A single '>' that is followed by something path-like.  '>>' (append), '2>&1', '->',
# '>=' and '<>' are all excluded, so an append never reads as a truncation.
_TRUNC_REDIRECT_RX = re.compile(r"(?<![>\-=!<])>(?!>)\s*['\"]?[\w.$~/\\{]")
_TRUNC_CMDLET_RX = re.compile(
    r"\bclear-content\b"
    r"|\bset-content\b"
    r"|\bout-file\b"
    r"|(?:^|[\s;&|`(])tee\b"
    r"|\bos\.truncate\s*\(",
    re.I,
)
_APPEND_FLAG_RX = re.compile(r"-append\b|(?:^|\s)tee\s+(?:[^|;]*\s)?-a\b", re.I)

# HOOK-FALSE-POSITIVE-1 -- WHAT a command truncates, not merely WHETHER it truncates.
# `_TRUNC_REDIRECT_RX`/`_TRUNC_CMDLET_RX` above answer "does this text truncate ANYTHING",
# which is all `shell_acts` needs; this answers "which TOKEN is written to", which is the
# only question NA-2's attribution may ask.  The two were conflated up to this delta, so a
# command that truncated ANYTHING and merely MENTIONED a protected path anywhere else on
# the line was refused as a truncating write OF that path -- measured 2026-09-07/08 on
# `cat <protected> 2>/dev/null` (the `2>` sets the act; the target is the bit bucket) and
# on a Python heredoc whose `if len(r) > 0` sets the act while the real write goes to an
# unprotected path.  Fourteen of twenty-seven headless editing runs died on that class.
#
# A file-descriptor dup is never a target: `&` is outside the destination character class,
# so `2>&1`, `>&2` and `1>&2` capture nothing at all.  `>>` is excluded by the same `(?!>)`
# the act regex uses, so an append is never read as a truncation.  A leading stream number
# (`1>`, `2>`) is NOT excluded -- `2> log.txt` really does truncate `log.txt`.
_TRUNC_DEST_RX = re.compile(r"(?<![>\-=!<])>(?!>)\s*(['\"][^'\"]+['\"]|[^\s;|&()<>]+)")
# The truncating cmdlets only -- `add-content` is an append and is deliberately absent.
_TRUNC_CMDLET_SEGMENT_RX = re.compile(
    r"\b(?:set-content|out-file|clear-content)\b[^\n;|&]*"
    r"|(?:^|[\s;&|`(])tee\b[^\n;|&]*"
    r"|\bos\.truncate\s*\([^\n;]*",
    re.I,
)


def truncating_destinations(command):
    """The tokens a truncating redirect or cmdlet actually WRITES TO.

    Interpreter method names (``write_bytes(``, ``write_text(``, ``open(..., 'w')``) are
    NOT shell truncation verbs and are not looked for here or in ``shell_acts``: an
    interpreter one-liner's effect is invisible to a text matcher BY DESIGN (see the limit
    recorded under the pre-flight artifact act), and inventing a verb for it denies the
    common read-then-write-elsewhere shape while still proving nothing about the target.
    """
    dests = []
    for match in _TRUNC_DEST_RX.finditer(command or ""):
        dests.append(match.group(1))
    for match in _TRUNC_CMDLET_SEGMENT_RX.finditer(command or ""):
        segment = match.group(0)
        if _APPEND_FLAG_RX.search(segment):
            continue
        dests.extend(
            token
            for token in tokens(segment)[1:]
            if not token.startswith("-") and ("/" in token or "\\" in token)
        )
    return dests


_GIT_RM_CACHED_RX = re.compile(r"\bgit\s+rm\b(?=[^\n;|&]*--cached\b)", re.I)


def shell_acts(command):
    """Which destructive act classes the command text exhibits.

    `git rm --cached` unstages and deletes nothing from the tree, so it is neutralised
    BEFORE the delete scan -- otherwise the bare `rm` alternative inside `git rm` matches
    first and the register's own carve-out never gets a chance to apply.
    """
    scan = _GIT_RM_CACHED_RX.sub(" gitrmcached ", command or "")
    acts = set()
    if _DELETE_RX.search(scan):
        acts.add("delete")
    if _MOVE_RX.search(scan):
        acts.add("move")
    if _TRUNC_REDIRECT_RX.search(scan):
        acts.add("truncate")
    if _TRUNC_CMDLET_RX.search(scan) and not _APPEND_FLAG_RX.search(scan):
        acts.add("truncate")
    return acts


# ------------------------------------------------------------------------- NA-1

_PUSH_RX = re.compile(r"\bgit\b[^\n;|&]*\bpush\b", re.I)
_FORCE_RX = re.compile(r"--force-with-lease(?:=\S*)?|--force\b|(?:^|\s)-[a-z]*f[a-z]*\b", re.I)
_FILTER_RX = re.compile(r"\bfilter-branch\b|\bfilter-repo\b", re.I)
_RESET_HARD_RX = re.compile(r"\bgit\b[^\n;|&]*\breset\b[^\n;|&]*--hard\b", re.I)
_PROTECTED_URL_RX = re.compile(r"layibabalola/mlv-app", re.I)


def rule_na1(ctx):
    text = ctx.subject
    if ctx.tool not in SHELL_TOOLS:
        return
    if _FILTER_RX.search(text):
        raise Deny("NA-1", "history rewrite (filter-branch/filter-repo) is never authorized")
    pushes = list(_PUSH_RX.finditer(text))
    if not pushes:
        return
    if _RESET_HARD_RX.search(text):
        raise Deny("NA-1", "`git reset --hard` followed by a push in one command rewrites history")
    for match in pushes:
        segment = text[match.start() :]
        segment = re.split(r"[\n;|&]", segment)[0]
        forced = bool(_FORCE_RX.search(segment))
        plus_refspec = any(
            token.startswith("+") and ":" in token for token in tokens(segment)
        )
        if not forced and not plus_refspec:
            continue
        targets = [norm(token) for token in tokens(segment)]
        protected = _PROTECTED_URL_RX.search(segment) or "fork" in targets
        # Fail closed: a forced push naming no remote resolves to a default this hook
        # cannot read, and the default on this board is the protected one.
        named_remote = any(
            token in ("origin", "upstream") for token in targets
        ) and not protected
        if protected or not named_remote:
            raise Deny(
                "NA-1",
                "force push or +refspec to the protected fork is never authorized",
            )


# ------------------------------------------------------------------------- NA-2


def _carve_tag(ctx, path_norm):
    """Which NA-2 carve-out (O47) a protected path sits on, if any."""
    rest = after_seg(path_norm, DUAL_TAIL)
    if rest is not None:
        if rest == KILL_SWITCH_LEAF:
            return "killswitch"
        if rest.startswith("receipts/"):
            return "receipts"
        if rest in ("queue.json", "lane-gh-capability.json"):
            return "carve-file"
        if rest.startswith("digest/"):
            return "digest"
        if "/" not in rest and rest.endswith("-resume-current.md"):
            return "resume"
        if rest.startswith("ignition/seat-") and rest.endswith(".md"):
            return "seat"
    if after_seg(path_norm, FLEET_TAIL) is not None:
        return "fleet"
    return None


def _is_na2_protected(path_norm):
    if any(has_seg(path_norm, tail) for tail in NA2_PROTECTED_TAILS):
        return True
    return has_seg(path_norm, NA2_PROTECTED_FILE_TAIL)


def _manifest_surface(ctx, path_norm):
    """O124: is this one of the four RATIFIED manifest surfaces of exception (iv)?

    FAIL-CLOSED READINGS (recorded, 0.05 fourth review delta):
      * The surfaces are pinned to THE BOARD's own ``$D`` -- ``ctx.dual_dir_raw`` -- not to
        any directory that merely ends ``.claude-state/coordination/dual-lane``.  A lane
        worktree cannot carry a copy of the tree and thereby earn the exception, and the
        exception's whole justification is that the amendment happens at the board.
      * ``$D/DEFINITIVE-FIX-PLAN-*.md`` is a BASENAME glob on a DIRECT CHILD of ``$D``: a
        matching name nested deeper is a different file and gets no exception.
      * ``prompts/v2/**`` is a prefix, so the cards below it are covered; ``prompts/`` and
        ``prompts/v1/`` are not.
    Returns the surface kind, or None.
    """
    rest = after_seg(path_norm, DUAL_TAIL)
    if rest is None or not rest:
        return None
    if not under(path_norm, norm(ctx.dual_dir_raw)):
        return None
    if "/" not in rest:
        if rest.startswith(MANIFEST_PLAN_GLOB_PREFIX) and rest.endswith(
            MANIFEST_PLAN_GLOB_SUFFIX
        ):
            return "plan"
        if rest in MANIFEST_EXACT_LEAVES:
            return "register" if rest.endswith(".json") else "wrapper"
        return None
    if rest.startswith(MANIFEST_PROMPTS_PREFIX) and len(rest) > len(
        MANIFEST_PROMPTS_PREFIX
    ):
        return "prompt"
    return None


def _is_sha256(value):
    return isinstance(value, str) and SHA256_HEX_RX.match(value) is not None


def _is_sha(value):
    return isinstance(value, str) and SHA_HEX_RX.match(value) is not None


def _is_url(value):
    return isinstance(value, str) and value.startswith(RECEIPT_URL_PREFIX)


def _is_int(value):
    # `bool` is a subclass of `int` and `True == 1`, so the bool test comes FIRST.  A
    # `frozenCount` of `true` is not a count, and this is the one class where the naive
    # `isinstance` reading admits a value of the wrong type outright.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_list(value):
    return isinstance(value, list)


def _is_nonempty(value):
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)) and not value:
        return False
    return True


def _is_contexts(value):
    """0.4b's `postContexts`: exactly the canonical five, and `Batch Compile` among them."""
    if not isinstance(value, list) or len(value) != CANONICAL_04B_CONTEXT_COUNT:
        return False
    if not all(isinstance(item, str) and item for item in value):
        return False
    return CANONICAL_04B_PROMOTED_CONTEXT in value


def _resolve_path_value(ctx, value):
    """`path`: the FILE a value names, read ABSOLUTE first and then relative to the board root.

    -> the path that exists, or ``None``.  Absolute first, so a receipt written with a full
    path is judged on the file it names; board-relative second, because that is how the
    plan's steps record these fields.  The fall-back is deliberately the BOARD and never the
    hook's cwd: a relative path resolved against whatever directory the session happened to
    be in would make the same receipt valid or invalid depending on who read it.  A
    directory is not a file: `os.path.isfile` is the test.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        if os.path.isfile(value):
            return value
        joined = os.path.join(ctx.board_root_raw, value)
        if os.path.isfile(joined):
            return joined
    except Exception:
        return None
    return None


def _path_value_exists(ctx, value):
    return _resolve_path_value(ctx, value) is not None


def _text_of(path):
    """-> the file's text (a BOM tolerated), or ``None`` when it cannot be read as UTF-8."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except Exception:
        return None


def _terminal_json_block(text):
    """-> ``(document, why)``: the TERMINAL JSON block of a verdict file (S120).

    The last ```json fenced block when the file carries any -- `sol-review-PR-TEMPLATE`
    ends with exactly one, and a review that quotes an earlier block in its prose still
    ENDS with its own -- else the last TOP-LEVEL JSON object in the file, which is what a
    verdict saved as plain `.json` is.  Top-level means a `{` at column 0 that decodes as
    an object and does not sit inside an object already decoded, so a pretty-printed
    member of the root is never mistaken for the root.  ``document`` is ``None`` and
    ``why`` says which reading failed when neither yields an object: a fence that does not
    parse is a malformed verdict, not an invitation to fall back to the other reading.
    """
    fences = _VERDICT_FENCE_RX.findall(text)
    if fences:
        try:
            document = json.loads(fences[-1])
        except ValueError:
            return None, "its last ```json block does not parse"
        if not isinstance(document, dict):
            return None, "its last ```json block is not a JSON object"
        return document, ""
    decoder = json.JSONDecoder()
    last = None
    consumed = 0
    for match in _VERDICT_OBJECT_HEAD_RX.finditer(text):
        if match.start() < consumed:
            continue
        try:
            candidate, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        consumed = end
        if isinstance(candidate, dict):
            last = candidate
    if last is None:
        return (
            None,
            "carries no terminal JSON block (no ```json fence and no top-level JSON object)",
        )
    return last, ""


def _value_class_ok(ctx, klass, value):
    if klass == "sha256":
        return _is_sha256(value)
    if klass == "sha":
        return _is_sha(value)
    if klass == "path":
        return _path_value_exists(ctx, value)
    if klass == "url":
        return _is_url(value)
    if klass == "int":
        return _is_int(value)
    if klass == "list":
        return _is_list(value)
    if klass == "contexts":
        return _is_contexts(value)
    if klass == "nonempty":
        return _is_nonempty(value)
    # An unknown class is a schema this hook does not implement, and guessing is how the
    # gate got soft in the first place.
    return False


_VALUE_CLASS_PROSE = {
    "sha256": "a 64-character LOWERCASE hex sha256",
    "sha": "a 40-character LOWERCASE hex sha",
    "path": "a path to a file that EXISTS (absolute, or relative to the board root)",
    "url": "a url beginning " + RECEIPT_URL_PREFIX,
    "int": "a NON-NEGATIVE integer (a boolean is not one)",
    "list": "a JSON array",
    "contexts": (
        "a JSON array of exactly %d non-empty strings containing %r"
        % (CANONICAL_04B_CONTEXT_COUNT, CANONICAL_04B_PROMOTED_CONTEXT)
    ),
    "nonempty": "present and NON-EMPTY",
}


def _receipt_shape_why(name, document, required):
    """-> a refusal naming FILE and KEY for a receipt that is not a stamped object, else ``""``.

    The three faults every gate receipt shares, in the order that makes a refusal
    attributable: parseable JSON, a NON-EMPTY object, and `recordedUtc` in the pinned
    notation.  The EMPTY-OBJECT case gets its own line naming the keys it carries none of,
    because `{}` is what presence-only validation accepted and the operator reading the
    refusal should be told that, not that one arbitrary key is missing.
    """
    if document is None:
        return "receipt %s is absent or is not parseable JSON (S118)" % name
    if not isinstance(document, dict):
        return "receipt %s is not a JSON object (S118)" % name
    if not document:
        return (
            "receipt %s is an EMPTY object -- it carries none of its required keys "
            "(%s), and an empty object is INVALID (S118)"
            % (name, ", ".join((RECORDED_UTC_KEY,) + tuple(required)))
        )
    return ""


def _recorded_utc_why(name, document, rule):
    """-> a refusal for a missing or non-conforming ``recordedUtc``, else ``""``.

    ONE stamp rule for all six gate receipts (O155 was rejected in favour of this).  The
    ``rule`` argument only chooses which id the line cites -- O141 for the chain, whose
    ordering the stamp decides, S118 for the five fixed receipts, whose stamp is a schema
    key like any other.
    """
    stamp = document.get(RECORDED_UTC_KEY)
    if not isinstance(stamp, str) or not stamp:
        return "receipt %s lacks %s (%s)" % (name, RECORDED_UTC_KEY, rule)
    if _parse_pinned_utc(stamp) is None:
        return (
            "receipt %s carries %s %r, which is not the ONE pinned notation %s -- whole "
            "seconds, uppercase Z, no fraction, no offset (%s)"
            % (name, RECORDED_UTC_KEY, stamp, PINNED_UTC_NOTATION, rule)
        )
    return ""


def _fixed_receipt_why(ctx, name, document, required):
    """-> a refusal naming the FILE and the KEY that failed this receipt's row, else ``""``."""
    why = _receipt_shape_why(name, document, [key for key, _ in required])
    if why:
        return why
    why = _recorded_utc_why(name, document, "S118")
    if why:
        return why
    for key, klass in required:
        if key not in document:
            return "receipt %s lacks the required key %s (S118)" % (name, key)
        value = document[key]
        if not _value_class_ok(ctx, klass, value):
            return (
                "receipt %s carries %s %r, which is not %s (S118)"
                % (name, key, value, _VALUE_CLASS_PROSE[klass])
            )
    return ""


def _control_approval_why(ctx, name, document):
    """S120/S123: the two shas, and the second key's APPROVE of exactly the REVIEWED one, else why.

    `reviewedHeadSha` is the PR head sol reviewed BEFORE the merge -- the sha the verdict's
    `subject_sha` is compared to.  `mergeSha` is the post-merge commit the hashes were taken
    at, judged for SHAPE only (S123): it is never compared to the verdict, because a GitHub
    merge lands a different commit and the verdict is of the reviewed head; it is never
    compared to `reviewedHeadSha` either way, because the hook does not know the merge
    method; and no git is consulted, because the fixed set hashing equal at both shas is
    the hub's assertion before the receipt exists.

    The verdict file is read through ``_terminal_json_block``: the last ```json fence, else
    the last top-level object.  Every arm is a refusal, never an exclusion (O152), and each
    names the receipt, the key and -- once the file is open -- the verdict path, so a
    near-miss is attributable to the one thing that failed: either key absent, either sha
    malformed, the path absent, the file unreadable or without a terminal block, a verdict
    other than APPROVE, or an APPROVE of some OTHER head, which is the second key's approval
    of something else.  The arms run in the plan's row order: `reviewedHeadSha`,
    `mergeSha`, `solVerdictPath`.
    """
    if CONTROL_REVIEWED_HEAD_KEY not in document:
        return "execution-control receipt %s lacks %s (S120/O152)" % (
            name,
            CONTROL_REVIEWED_HEAD_KEY,
        )
    head = document[CONTROL_REVIEWED_HEAD_KEY]
    if not _is_sha(head):
        return "execution-control receipt %s carries %s %r, which is not %s (S120/O152)" % (
            name,
            CONTROL_REVIEWED_HEAD_KEY,
            head,
            _VALUE_CLASS_PROSE["sha"],
        )
    if CONTROL_MERGE_SHA_KEY not in document:
        return "execution-control receipt %s lacks %s (S123/O152)" % (
            name,
            CONTROL_MERGE_SHA_KEY,
        )
    merge = document[CONTROL_MERGE_SHA_KEY]
    if not _is_sha(merge):
        return "execution-control receipt %s carries %s %r, which is not %s (S123/O152)" % (
            name,
            CONTROL_MERGE_SHA_KEY,
            merge,
            _VALUE_CLASS_PROSE["sha"],
        )
    if CONTROL_VERDICT_PATH_KEY not in document:
        return "execution-control receipt %s lacks %s (S120/O152)" % (
            name,
            CONTROL_VERDICT_PATH_KEY,
        )
    verdict_path = document[CONTROL_VERDICT_PATH_KEY]
    resolved = _resolve_path_value(ctx, verdict_path)
    if resolved is None:
        return "execution-control receipt %s carries %s %r, which is not %s (S120/O152)" % (
            name,
            CONTROL_VERDICT_PATH_KEY,
            verdict_path,
            _VALUE_CLASS_PROSE["path"],
        )
    text = _text_of(resolved)
    if text is None:
        return (
            "execution-control receipt %s carries %s %r, and that file could not be read "
            "as UTF-8 text (S120/O152)" % (name, CONTROL_VERDICT_PATH_KEY, verdict_path)
        )
    verdict, why = _terminal_json_block(text)
    if verdict is None:
        return "execution-control receipt %s carries %s %r, and that file %s (S120/O152)" % (
            name,
            CONTROL_VERDICT_PATH_KEY,
            verdict_path,
            why,
        )
    if verdict.get(CONTROL_VERDICT_KEY) != CONTROL_VERDICT_APPROVE:
        return (
            "execution-control receipt %s carries %s %r, whose terminal JSON block carries "
            "%s %r, not %r -- the second key has not approved this head (S120/O152)"
            % (
                name,
                CONTROL_VERDICT_PATH_KEY,
                verdict_path,
                CONTROL_VERDICT_KEY,
                verdict.get(CONTROL_VERDICT_KEY),
                CONTROL_VERDICT_APPROVE,
            )
        )
    if verdict.get(CONTROL_VERDICT_SUBJECT_KEY) != head:
        return (
            "execution-control receipt %s carries %s %r, whose terminal JSON block carries "
            "%s %r, not this receipt's %s %r -- an approval of a DIFFERENT head is not the "
            "second key's approval of this one (S120/O152)"
            % (
                name,
                CONTROL_VERDICT_PATH_KEY,
                verdict_path,
                CONTROL_VERDICT_SUBJECT_KEY,
                verdict.get(CONTROL_VERDICT_SUBJECT_KEY),
                CONTROL_REVIEWED_HEAD_KEY,
                head,
            )
        )
    return ""


def _control_hashes_why(name, document):
    """S120: `hashes` is an object whose KEY SET is EXACTLY the step's fixed set, else why.

    Missing keys are named before extra ones, and both before the value class, so a receipt
    that is wrong in more than one way is attributed to the plan's first complaint.  The set
    is read off ``EXECUTION_CONTROL_REQUIRED_HASHES`` by the receipt's NAME; a chain name
    with no row there is a table this hook does not implement, and that is a refusal too.
    """
    if CONTROL_HASHES_KEY not in document:
        return "execution-control receipt %s lacks %s (S118/O152)" % (
            name,
            CONTROL_HASHES_KEY,
        )
    hashes = document[CONTROL_HASHES_KEY]
    if not isinstance(hashes, dict) or not hashes:
        return (
            "execution-control receipt %s carries %s %r, which is not a NON-EMPTY object "
            "mapping each hashed path to its sha256 (S118/O152)"
            % (name, CONTROL_HASHES_KEY, hashes)
        )
    required = EXECUTION_CONTROL_REQUIRED_HASHES.get(name)
    if required is None:
        return (
            "execution-control receipt %s has no row in the fixed hash-set table, so its "
            "%s cannot be judged (S120/O152)" % (name, CONTROL_HASHES_KEY)
        )
    missing = sorted(required - frozenset(hashes))
    if missing:
        return (
            "execution-control receipt %s carries %s lacking %s -- the key set must be "
            "EXACTLY this step's fixed set of %d paths, and a missing key is a file the "
            "step did not measure (S120/O152)"
            % (name, CONTROL_HASHES_KEY, ", ".join(missing), len(required))
        )
    extra = sorted(frozenset(hashes) - required)
    if extra:
        return (
            "execution-control receipt %s carries %s with %s, which is not in this step's "
            "fixed set -- the key set must be EXACTLY that set of %d paths, and an extra "
            "key is a file no step landed (S120/O152)"
            % (name, CONTROL_HASHES_KEY, ", ".join(extra), len(required))
        )
    for hashed_path in sorted(hashes):
        if not _is_sha256(hashes[hashed_path]):
            return (
                "execution-control receipt %s maps %s entry %r to %r, which is not %s "
                "(S118/O152)"
                % (
                    name,
                    CONTROL_HASHES_KEY,
                    hashed_path,
                    hashes[hashed_path],
                    _VALUE_CLASS_PROSE["sha256"],
                )
            )
    return ""


def _fixed_set_digest(hashes):
    """O172: the digest of a ``hashes`` object in the ONE canonical form.

    One line per entry: ``<sha256>  <path>`` -- the digest, TWO spaces, the board-relative
    path, the shape ``sha256sum`` prints.  The lines are SORTED as plain strings (so by
    digest first, the order ``sort`` gives ``sha256sum`` output), joined by a single LF with
    NO trailing newline, encoded UTF-8 and hashed with sha256; the result is lowercase hex.
    Nothing else is hashed -- not the receipt, not the file bytes, not git -- so the digest
    is a function of ``hashes`` alone and any writer that follows this paragraph gets the
    same value.
    """
    lines = sorted("%s  %s" % (hashes[path], path) for path in hashes)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _control_equality_proof_why(name, document):
    """O172: ``fixedSetEqualityProof`` binds BOTH shas to the digest of this receipt's OWN ``hashes``, else why.

    The literal is ``<reviewedHeadSha>=<mergeSha>:<digest>`` -- two 40-character lowercase
    hex shas joined by ``=``, then ``:`` and a 64-character lowercase hex sha256.  Three
    arms, in the order that makes a refusal attributable: the SHAPE; the two shas, which
    must EQUAL this receipt's ``reviewedHeadSha`` and ``mergeSha`` in that order; and the
    digest, which must equal ``_fixed_set_digest`` RECOMPUTED from this receipt's own
    ``hashes`` -- pure recomputation, no git, no file read -- so a proof pasted from another
    receipt, or written before the hashes were, fails by value.  What the proof ATTESTS,
    that the step's fixed set hashed equal at both shas, is the hub's assertion made with
    git before the receipt is written; the hook checks that the attestation is bound to
    this receipt's shas and this receipt's hashes, and nothing about git.  Runs AFTER the
    ``hashes`` arm, because the digest is recomputed from that object and a refusal about
    the proof of a malformed ``hashes`` would name the wrong key.
    """
    if CONTROL_EQUALITY_PROOF_KEY not in document:
        return "execution-control receipt %s lacks %s (O172/O152)" % (
            name,
            CONTROL_EQUALITY_PROOF_KEY,
        )
    proof = document[CONTROL_EQUALITY_PROOF_KEY]
    match = _EQUALITY_PROOF_RX.match(proof) if isinstance(proof, str) else None
    if match is None:
        return (
            "execution-control receipt %s carries %s %r, which is not the literal "
            "<reviewedHeadSha>=<mergeSha>:<digest> -- two 40-character LOWERCASE hex shas "
            "joined by '=', then ':' and a 64-character LOWERCASE hex sha256 (O172/O152)"
            % (name, CONTROL_EQUALITY_PROOF_KEY, proof)
        )
    if match.group("reviewed") != document[CONTROL_REVIEWED_HEAD_KEY]:
        return (
            "execution-control receipt %s carries %s whose FIRST sha %r is not this "
            "receipt's %s %r (O172/O152)"
            % (
                name,
                CONTROL_EQUALITY_PROOF_KEY,
                match.group("reviewed"),
                CONTROL_REVIEWED_HEAD_KEY,
                document[CONTROL_REVIEWED_HEAD_KEY],
            )
        )
    if match.group("merge") != document[CONTROL_MERGE_SHA_KEY]:
        return (
            "execution-control receipt %s carries %s whose SECOND sha %r is not this "
            "receipt's %s %r (O172/O152)"
            % (
                name,
                CONTROL_EQUALITY_PROOF_KEY,
                match.group("merge"),
                CONTROL_MERGE_SHA_KEY,
                document[CONTROL_MERGE_SHA_KEY],
            )
        )
    digest = _fixed_set_digest(document[CONTROL_HASHES_KEY])
    if match.group("digest") != digest:
        return (
            "execution-control receipt %s carries %s whose digest %r is not the sha256 of "
            "the sorted '<sha256>  <path>' lines of its OWN hashes, which is %r -- a proof "
            "is bound to the hashes beside it, never pasted from another receipt (O172/O152)"
            % (name, CONTROL_EQUALITY_PROOF_KEY, match.group("digest"), digest)
        )
    return ""


def _control_step_keys_why(ctx, name, document):
    """S120: the keys ONE step carries beyond the common ones, else why.

    0.1 carries `composerStatus` == `not-yet-created` and the three evidence paths; 0.35
    carries the three paths.  Every other step carries neither, and a key the plan does not
    list for a step is not judged here -- the exact-set rule is a rule about `hashes`.
    """
    if name in EXECUTION_CONTROL_COMPOSER_STATUS_NAMES:
        if CONTROL_COMPOSER_STATUS_KEY not in document:
            return "execution-control receipt %s lacks %s (S120/O152)" % (
                name,
                CONTROL_COMPOSER_STATUS_KEY,
            )
        if document[CONTROL_COMPOSER_STATUS_KEY] != CONTROL_COMPOSER_STATUS_VALUE:
            return (
                "execution-control receipt %s carries %s %r, not exactly %r (S120/O152)"
                % (
                    name,
                    CONTROL_COMPOSER_STATUS_KEY,
                    document[CONTROL_COMPOSER_STATUS_KEY],
                    CONTROL_COMPOSER_STATUS_VALUE,
                )
            )
    if name in EXECUTION_CONTROL_COMPOSER_PATH_NAMES:
        for key in CONTROL_COMPOSER_PATH_KEYS:
            if key not in document:
                return "execution-control receipt %s lacks %s (S120/O152)" % (name, key)
            if not _path_value_exists(ctx, document[key]):
                return (
                    "execution-control receipt %s carries %s %r, which is not %s (S120/O152)"
                    % (name, key, document[key], _VALUE_CLASS_PROSE["path"])
                )
    return ""


def _control_receipt_why(ctx, name, document, parity_sha):
    """-> a refusal naming the FILE and the KEY for a chain receipt, else ``""``.

    O152, THE STRICT RULE.  Every arm here returns a refusal rather than an exclusion: a
    chain receipt that is PRESENT but invalid makes the newest UNDECIDABLE and fails the
    exception closed.  Excluding it instead would re-open O141's hazard in a new shape --
    an invalid 0.7 beside a valid 0.6 would silently select the STALE 0.6 and the one-shot
    enable would fire against a superseded receipt.  Recovery is a NON-shrinking `Write`
    rewrite of the offending receipt carrying a conforming stamp and every required key,
    which the receipts carve-out allows; never a delete.

    The arms run in the plan's row order -- shape, stamp, the two shas and the second key's
    approval of the reviewed one (S120/S123), the fixed hash set (S120), the equality proof
    bound to both shas and to that set (O172 -- after the set, because its digest is
    recomputed from it), the step's own keys (S120), the provenance (S118) -- and each
    returns the FIRST failure, so a receipt with two faults is attributed to the one the
    plan lists first.
    """
    why = _receipt_shape_why(
        name,
        document,
        (
            CONTROL_REVIEWED_HEAD_KEY,
            CONTROL_MERGE_SHA_KEY,
            CONTROL_VERDICT_PATH_KEY,
            CONTROL_HASHES_KEY,
            CONTROL_EQUALITY_PROOF_KEY,
        ),
    )
    if why:
        return why
    # The stamp keeps the O141 wording and the O141 id: it is the arm that decides the
    # ORDER, and the falsifiers that measured it name that id.
    stamp = document.get(RECORDED_UTC_KEY)
    if not isinstance(stamp, str) or not stamp:
        return "execution-control receipt %s lacks %s (O105/O152)" % (
            name,
            RECORDED_UTC_KEY,
        )
    if _parse_pinned_utc(stamp) is None:
        return (
            "execution-control receipt %s carries %s %r, which is not the ONE pinned "
            "notation %s -- whole seconds, uppercase Z, no fraction, no offset -- so the "
            "NEWEST is UNDECIDABLE and the exception fails closed (O141)"
            % (name, RECORDED_UTC_KEY, stamp, PINNED_UTC_NOTATION)
        )
    why = _control_approval_why(ctx, name, document)
    if why:
        return why
    why = _control_hashes_why(name, document)
    if why:
        return why
    why = _control_equality_proof_why(name, document)
    if why:
        return why
    why = _control_step_keys_why(ctx, name, document)
    if why:
        return why
    if name in EXECUTION_CONTROL_PROVENANCE_EXEMPT:
        return ""
    for key in PROVENANCE_KEYS:
        if key not in document:
            return "execution-control receipt %s lacks %s (S118/O152)" % (name, key)
    parity = document[PROVENANCE_PARITY_KEY]
    if not _is_sha256(parity):
        return (
            "execution-control receipt %s carries %s %r, which is not %s (S118/O152)"
            % (name, PROVENANCE_PARITY_KEY, parity, _VALUE_CLASS_PROSE["sha256"])
        )
    if parity != parity_sha:
        # BOUND, not merely well-formed.  A digest of the right SHAPE that is a digest of
        # nothing is exactly what the forged set carried.
        return (
            "execution-control receipt %s carries %s %r, which is not the sha256 of %s as "
            "it is on disk (%r) (S118/O152)"
            % (name, PROVENANCE_PARITY_KEY, parity, ROADMAP_PARITY_RECEIPT, parity_sha)
        )
    if not _is_sha256(document[PROVENANCE_QUEUE_KEY]):
        return (
            "execution-control receipt %s carries %s %r, which is not %s (S118/O152)"
            % (
                name,
                PROVENANCE_QUEUE_KEY,
                document[PROVENANCE_QUEUE_KEY],
                _VALUE_CLASS_PROSE["sha256"],
            )
        )
    if document[PROVENANCE_COUNT_KEY] != PROVENANCE_PRODUCT_LIVE_COUNT or isinstance(
        document[PROVENANCE_COUNT_KEY], bool
    ):
        return (
            "execution-control receipt %s carries %s %r, not exactly %d (S118/O152)"
            % (
                name,
                PROVENANCE_COUNT_KEY,
                document[PROVENANCE_COUNT_KEY],
                PROVENANCE_PRODUCT_LIVE_COUNT,
            )
        )
    return ""


def _kill_switch_receipts_ok(ctx):
    """The RECEIPT half of exception (i): every named 0.2 receipt validates, enable unspent.

    -> ``(ok, why, newest_control_name)``.  The THIRD member is the BASENAME this function
    selected, and it exists so S112's literal check can be bound to the SAME selection that
    decided whether the exception is open at all.  A second implementation of "newest by
    ``recordedUtc``" living next to this one is a second answer waiting to disagree with it:
    the register names one newest receipt, so the hook computes it once.  It is ``""``
    whenever ``ok`` is False -- there is no newest to name when the set does not validate.

    Reached ONLY from the canonical dedicated act below (S99/O118).  It is no longer a
    standing key that any input naming the marker under a delete verb can turn: a delete
    outside the canonical compound is refused before this is ever consulted.

    S118 -- "VALIDATE" IS THE SCHEMA TABLE, NOT ``json.load(...) is not None``.  Each of the
    five fixed receipts is judged against its row of ``KILL_SWITCH_RECEIPT_SCHEMAS``:
    required keys, value classes, and ``recordedUtc`` in the pinned notation.  An EMPTY
    OBJECT is INVALID -- which is the whole point, because the hub's reproduction on the
    sixth commit was five ``{}`` receipts beside ``execution-control-forged.json`` carrying
    only the provenance keys, and that set ALLOWED the one-shot enable.  Every refusal names
    the FILE and the KEY (or the value class) that failed.

    The execution-control arm's candidate set is the SIX CHAIN NAMES and no other; any other
    ``execution-control-*.json`` present is INVALID and fails the exception closed by name.
    Each chain receipt carries ``reviewedHeadSha`` (the PR head reviewed BEFORE the merge)
    and ``mergeSha`` (the post-merge commit its hashes were taken at -- S123, shape only,
    never compared to each other and never to git), a ``solVerdictPath`` whose terminal
    JSON block is the second key's APPROVE of exactly the REVIEWED head (S120), ``hashes``
    whose KEY SET is exactly its step's fixed set with sha256 values (S120), its step's own
    keys, and,
    from 0.35 on, ``roadmapParityReceiptSha256`` EQUAL to the sha256 of
    ``0.18-roadmap-parity.json`` as it is on disk, a well-formed ``queueSha256``, and
    ``productLiveCount`` exactly 15.  The O158 re-hash of the SELECTED receipt's fixed set
    runs in ``_enable_act_allowed``, after the venue test, because its refusal names what
    drifted in the board root and that is the board-rooted actor's to see.

    O141/O152 pin the ORDER and the STRICTNESS.  A receipt LACKING ``recordedUtc`` is INVALID
    (O105) and its mere presence makes the newest undecidable, so the whole exception fails
    closed; so does a receipt whose ``recordedUtc`` is not the ONE pinned notation
    ``YYYY-MM-DDTHH:MM:SSZ``, and that refusal NAMES the offending file -- a non-conforming
    stamp is never silently skipped and never treated as older.  O152 generalises that to
    EVERY schema fault: a chain receipt that is PRESENT but invalid is never excluded, it
    makes the newest UNDECIDABLE, because excluding it would let an invalid 0.7 beside a
    valid 0.6 select the STALE 0.6.  Recovery is a NON-shrinking ``Write`` rewrite of the
    offending receipt, never a delete.  The order is the order of the PARSED values, never
    of the raw bytes, and an EXACT TIE between two candidates is undecidable too: DENY,
    naming BOTH files.

    The SELECTED receipt is a NAME -- ``execution-control-0.7.json`` when a valid one exists,
    else ``execution-control-0.6.json`` -- and the parsed-newest valid chain receipt must BE
    that one.  A valid 0.6 stamped later than a valid 0.7 is a CHAIN VIOLATION, undecidable
    rather than a new answer; the selected receipt absent is DENY.

    S98 -- THE ENABLE IS ONE-SHOT, and this is the first arm because it is the decisive
    one.  Exception (i) opens only while ``receipts/0.2-loop-enabled.json`` is ABSENT.  0.2
    creates that receipt in the same guarded action as the delete, so once it exists the
    single ratified authorization has been spent: a RE-ARMED marker (creating the kill
    switch is always allowed, exception (iii)) cannot then be deleted again without a NEWLY
    ratified authorization.  Without this arm the six receipts would be a standing key --
    valid forever, re-usable on every re-arm -- which is a gate that opens once and never
    closes.  Fail-closed: PRESENCE of the path is enough; it is not parsed, so an
    unreadable or truncated enable receipt still refuses.
    """
    enable_receipt = ctx.receipt(KILL_SWITCH_ENABLE_RECEIPT)
    if os.path.exists(enable_receipt):
        return (
            False,
            "the 0.2 enable is ONE-SHOT and receipts/%s is PRESENT, so this authorization "
            "is spent -- a re-armed marker needs a newly ratified authorization (S98)"
            % KILL_SWITCH_ENABLE_RECEIPT,
            "",
        )
    # S118 -- THE FIVE FIXED RECEIPTS VALIDATE AGAINST THE SCHEMA TABLE, one row each.
    # `0.18-roadmap-parity.json` is hashed as soon as it validates, because every chain
    # receipt from 0.35 on must carry THAT digest: the binding is to the bytes on disk, so
    # it is computed from the file and never trusted from a receipt.
    parity_sha = None
    for name, required in KILL_SWITCH_RECEIPT_SCHEMAS:
        path = ctx.receipt(name)
        if not os.path.exists(path):
            return False, "receipt %s is absent (S118)" % name, ""
        why = _fixed_receipt_why(ctx, name, read_json(path), required)
        if why:
            return False, why, ""
        if name == ROADMAP_PARITY_RECEIPT:
            parity_sha = _sha256_of(path)
            if parity_sha is None:
                return (
                    False,
                    "receipt %s could not be read to hash it, so no chain receipt's %s "
                    "can be bound to it (S118)"
                    % (name, PROVENANCE_PARITY_KEY),
                    "",
                )
    try:
        names = sorted(os.listdir(ctx.receipts_dir_raw))
    except Exception:
        return False, "receipts directory is unreadable", ""
    controls = [
        name
        for name in names
        if name.startswith(EXECUTION_CONTROL_PREFIX)
        and name.endswith(EXECUTION_CONTROL_SUFFIX)
    ]
    if not controls:
        return False, "no execution-control receipt is present", ""
    # S118 -- THE CANDIDATE SET IS AN ENUMERATION, NOT A GLOB, and a name outside it fails
    # the exception CLOSED rather than being skipped.  The hub's reproduction turned on
    # exactly this: `execution-control-forged.json`, a name no plan step writes, joined the
    # selection because the filter was `execution-control-*.json`.
    for name in controls:
        if name not in EXECUTION_CONTROL_CHAIN:
            return (
                False,
                "execution-control receipt %s is not one of the SIX chain names (%s), so "
                "the candidate set is not the chain and the exception fails closed (S118)"
                % (name, ", ".join(EXECUTION_CONTROL_CHAIN)),
                "",
            )
    # O141 -- ONE NOTATION, AND THE NEWEST IS THE PARSED VALUE.
    #
    # A non-conforming stamp is INVALID and makes the newest UNDECIDABLE, so it is refused
    # BY NAME: never silently skipped (a skipped receipt is one the board cannot see, and
    # the enable would then be taken against a set nobody enumerated) and never treated as
    # older -- which is what a raw-byte comparison did.  The hub measured
    # `'2026-09-06T16:00:00Z' > '2026-09-06T16:00:00.500000Z'` as True on the sixth commit,
    # selecting the STALE 0.6 as newest and letting the one-shot enable fire on it.  An
    # EXACT TIE is undecidable for the same reason, and its refusal names BOTH files.
    #
    # `controls` is sorted by NAME, so a comparison that quietly fell back to insertion
    # order would look right on any fixture whose names ascend with their stamps.  The
    # comparison is on `moment` and on nothing else.
    newest = None
    stamped = {}
    for name in controls:
        # O152, STRICT.  EVERY present chain receipt is validated in FULL -- shape, stamp,
        # `hashes`, and from 0.35 on the provenance bound to the on-disk 0.18 digest -- and
        # any failure refuses HERE, naming the file and the key.  This is what "never
        # silently excluded" means operationally: the loop has no `continue`.
        document = read_json(ctx.receipt(name))
        why = _control_receipt_why(ctx, name, document, parity_sha)
        if why:
            return False, why, ""
        moment = _parse_pinned_utc(document.get(RECORDED_UTC_KEY))
        if moment in stamped:
            return (
                False,
                "execution-control receipts %s and %s carry the SAME recordedUtc %r, so "
                "the NEWEST is UNDECIDABLE and the exception fails closed (O141)"
                % (stamped[moment], name, document[RECORDED_UTC_KEY]),
                "",
            )
        stamped[moment] = name
        if newest is None or moment > newest[0]:
            newest = (moment, name, document)
    # S118 -- THE SELECTED RECEIPT IS A NAME.  Every present chain receipt validated above,
    # so "a valid 0.7 exists" is "0.7 is present" here and nowhere else: under O152's strict
    # rule an invalid 0.7 already refused, rather than demoting the selection to 0.6.
    selected = (
        CONTROL_SELECTED_PREFERRED
        if CONTROL_SELECTED_PREFERRED in controls
        else CONTROL_SELECTED_FALLBACK
    )
    if selected not in controls:
        return (
            False,
            "the SELECTED execution-control receipt %s is ABSENT (the selection is %s when "
            "a valid one exists, else %s), so the exception fails closed (S118)"
            % (selected, CONTROL_SELECTED_PREFERRED, CONTROL_SELECTED_FALLBACK),
            "",
        )
    if newest[1] != selected:
        return (
            False,
            "the parsed-newest valid execution-control receipt is %s, but the SELECTED one "
            "is %s -- a CHAIN VIOLATION, so the newest is UNDECIDABLE and the exception "
            "fails closed (S118)" % (newest[1], selected),
            "",
        )
    return True, "", newest[1]


# ------------------------------------------- NA-2 exception (i): ONE DEDICATED ACT
#
# S99/O118.  Generic NA-2 attribution applies a command's ACT SET to EVERY path in its
# text, so the register's own canonical enable -- which creates the receipt AND deletes
# the marker in one input -- reads as a DELETE of a receipt and is refused (the
# Claude-family ratifier measured 8/8 formulations denied).  The resolution is not to
# loosen the attribution, which would also admit `Remove-Item <receipt>` on its own: the
# hook recognises the canonical compound as ONE dedicated act, BEFORE attribution runs,
# and the delete of the marker is authorized ONLY inside that act.  Everything else that
# deletes the marker -- a bare delete, a re-ordered compound, a different write cmdlet --
# is DENIED, receipts or no receipts.
#
# S101 -- THE CANONICAL SHAPE IS THE FAIL-CLOSED ONE, AND ONLY IT (0.05 fourth review
# delta).  `Set-Content` raises a NON-terminating error by default: on a full disk, a
# denied ACL or a missing parent the write fails, the pipeline continues, and the rev-18
# shape's `Remove-Item` then disarms the kill switch with `0.2-loop-enabled.json` ABSENT.
# That is not a degraded enable, it is the never-authorized act with a receipt-shaped alibi
# -- and worse, the S98 one-shot never closes, because the receipt whose presence spends
# the authorization was never written.  So the register now pins `-ErrorAction Stop` on
# BOTH acts and a read-back `throw` between them, and this hook matches ONLY that form.
# The rev-18 shape (no `-ErrorAction Stop`, no read-back) is NOT a grandfathered enable: it
# is an ordinary non-canonical input naming both paths, refused below with S101 named in
# the line so the operator is told WHICH degree of freedom closed.  Measured: the compound
# against a receipt path under a nonexistent directory exits non-zero at the write and
# LEAVES THE MARKER IN PLACE (the S101 acceptance, run for real against pwsh in
# `EnableCompoundIsFailClosedTests`).
#
# O128 -- THE COMPOUND NOW OPENS WITH `$ErrorActionPreference = 'Stop'` (0.05 FIFTH review
# delta), because `-ErrorAction Stop` turned out NOT to be the load-bearing arm for one
# whole failure class.  The hub measured the rev-19 shape in pwsh 7.6.5: a missing parent
# directory, a read-only target and a directory target each exit 1 with the marker PRESENT
# -- but a receipt path on an UNRESOLVABLE DRIVE exits 0, writes nothing, and DELETES THE
# MARKER.  The reason is binding, not writing: when the provider cannot resolve the drive
# the cmdlet's provider dynamic parameters never bind, `-ErrorAction` goes unbound with
# them, and the statement's failure is not governed by the parameter that was supposed to
# govern it.  A preference variable is not a parameter and cannot come unbound, so the
# leading statement closes the class the three parameter-level arms cannot reach.  With it,
# all four measured failures exit non-zero with the marker in place.  The rev-19 shape (no
# leading preference) is now what the rev-18 shape became under S101: an ordinary
# non-canonical input naming both paths, refused with O128 named in the line.
#
# FAIL-CLOSED READINGS OF THE CANONICAL SHAPE (recorded, 0.05 third, fourth and fifth review
# deltas).  The register pins the form character-for-character except whitespace, so every
# degree of freedom it does not name is refused rather than guessed:
#   * PowerShell tool ONLY -- the register writes "the canonical compound form" in
#     PowerShell; the same text arriving as a Bash input is not it.
#   * `$ErrorActionPreference = 'Stop'` FIRST, before anything else in the input.  The
#     preference NAME and VALUE are compared case-insensitively, because PowerShell resolves
#     a variable name and an `ActionPreference` enum member that way and a case difference
#     therefore changes no behaviour -- the same reading already applied to the two
#     `-ErrorAction Stop` arms.  Any OTHER value (`Continue`, `SilentlyContinue`) is refused:
#     it is the value that closes the drive class, not the statement.
#   * The variable is `$r` (case-insensitively, as PowerShell resolves it) in ALL THREE
#     places -- the assignment, `-Value`, and the read-back's `-cne` operand -- and nothing
#     else.  A verification that compares a DIFFERENT variable verifies nothing.
#   * `Set-Content -LiteralPath <receipt> -Value $r -NoNewline -Encoding utf8
#     -ErrorAction Stop` exactly: not `-Path`, not a reordered, dropped or extra parameter,
#     not `Out-File`, not a redirect.
#   * The read-back names THE SAME receipt path as the write, everywhere the path appears.
#     Reading a different file back proves the write succeeded somewhere else, which is
#     precisely the failure S101 exists to catch.
#   * The `throw` string is `enable-receipt-write-verification-failed`, compared
#     CASE-SENSITIVELY: the register pins a literal, and a literal that differs is a
#     different act -- the receipt of a halt is what a later reader greps for.
#   * `Remove-Item -LiteralPath <marker> -ErrorAction Stop` -- without it a delete failure
#     is swallowed and the enable reports success it did not have.
#   * Single-quoted literals containing no quote of their own; a PowerShell-escaped `''`
#     inside one fails the shape and is refused.
#   * `;` between the five statements, and NOTHING before, between or after them.
# The cost of each reading is a visible DENY on a near-miss, one line to fix.  The cost of
# the other reading is a silent ALLOW of a marker delete, which is the act itself.
#
# S112 -- AND THE LITERAL ITSELF IS READ, NOT COUNTED (0.05 SIXTH review delta).  Every
# reading above is about the SHAPE of the compound; until this delta the JSON it carries was
# checked only for five non-empty strings, so `{"state":"enabling","enabledUtc":"x",
# "executionControlReceipt":"x","executionControlSha256":"x","recordedUtc":"x"}` opened the
# gate and wrote itself down as the receipt of record.  The literal is now BOUND TO THE
# BOARD -- the newest execution-control receipt's basename, that file's lowercase sha256, and
# two stamps in the ONE pinned notation (O141) -- in `_enable_literal_ok` below.  A receipt
# that names nothing is not a lesser receipt; it is the enable act with a receipt-shaped
# alibi, the same failure class S101 closed at the write and S98 closed on re-arm.
_ENABLE_COMPOUND_RX = re.compile(
    r"\A\s*"
    r"\$(?P<pref>[A-Za-z_]\w*)\s*=\s*'(?P<prefvalue>[^']*)'\s*;\s*"
    r"\$(?P<var>[A-Za-z_]\w*)\s*=\s*'(?P<literal>[^']*)'\s*;\s*"
    r"Set-Content\s+-LiteralPath\s+'(?P<receipt>[^']*)'\s+-Value\s+\$(?P<value>[A-Za-z_]\w*)"
    r"\s+-NoNewline\s+-Encoding\s+utf8\s+-ErrorAction\s+Stop\s*;\s*"
    r"if\s*\(\s*\(\s*Get-Content\s+-LiteralPath\s+'(?P<readback>[^']*)'\s+-Raw"
    r"\s+-ErrorAction\s+Stop\s*\)\s*-cne\s+\$(?P<verify>[A-Za-z_]\w*)\s*\)\s*"
    r"\{\s*throw\s+'(?P<throw>[^']*)'\s*\}\s*;\s*"
    r"Remove-Item\s+-LiteralPath\s+'(?P<marker>[^']*)'\s+-ErrorAction\s+Stop"
    r"\s*\Z",
    re.I,
)


_ENABLE_PREFERENCE_HEAD_RX = re.compile(
    r"\A\s*\$" + ENABLE_PREFERENCE_VARIABLE + r"\s*=\s*'(?P<value>[^']*)'\s*;", re.I
)


def _enable_preference_gap(command):
    """O128: does this near-miss differ from the canonical shape in the LEADING preference?

    Returns a one-clause diagnosis, or ``""`` when the input DOES open with
    ``$ErrorActionPreference = 'Stop'`` and the near-miss is somewhere else.  Worth the few
    lines: the preference arm is the newest one, so a compound written against the rev-19
    register is the near-miss most likely to arrive in the field, and it also keeps ``O128``
    OUT of the refusal line for the inputs that already carry the preference -- so the S101
    rows and the O128 rows are separately attributable instead of sharing one catch-all.
    """
    head = _ENABLE_PREFERENCE_HEAD_RX.match(command or "")
    if head is None:
        return "; this input does not OPEN with $ErrorActionPreference = 'Stop' (O128)"
    if head.group("value").strip().lower() != ENABLE_PREFERENCE_VALUE:
        return "; this input opens with a preference of '%s', not 'Stop' (O128)" % (
            head.group("value"),
        )
    return ""


def _marker_path_norm(ctx):
    return norm(os.path.join(ctx.dual_dir_raw, KILL_SWITCH_MARKER_NAME))


def _enable_receipt_path_norm(ctx):
    return norm(ctx.receipt(KILL_SWITCH_ENABLE_RECEIPT))


def _canonical_enable_literal(ctx):
    """The JSON literal of the canonical enable compound, or None for any other shape."""
    if ctx.tool != "PowerShell":
        return None
    match = _ENABLE_COMPOUND_RX.match(ctx.command or "")
    if not match:
        return None
    # O128: the LEADING preference statement, and it must be the preference variable set to
    # `Stop`.  `$anythingElse = 'Stop'` opening the compound is not the arm that closes the
    # unbound-parameter class, and `$ErrorActionPreference = 'Continue'` is that arm turned
    # off.  Both are refused.
    if match.group("pref").lower() != ENABLE_PREFERENCE_VARIABLE:
        return None
    if match.group("prefvalue").strip().lower() != ENABLE_PREFERENCE_VALUE:
        return None
    variable = match.group("var").lower()
    if variable != ENABLE_LITERAL_VARIABLE or match.group("value").lower() != variable:
        return None
    # S101: `$r` in ALL THREE places.  A read-back that compares some other variable is a
    # verification of nothing, and it would pass every time.
    if match.group("verify").lower() != variable:
        return None
    receipt_norm = _enable_receipt_path_norm(ctx)
    if norm(match.group("receipt")) != receipt_norm:
        return None
    # S101: the read-back reads back THE RECEIPT THIS COMPOUND JUST WROTE.  A different
    # path would prove some other file's contents and let the delete run regardless.
    if norm(match.group("readback")) != receipt_norm:
        return None
    # S101: the pinned halt message, compared case-sensitively -- see the shape notes.
    if match.group("throw") != ENABLE_THROW_MESSAGE:
        return None
    if norm(match.group("marker")) != _marker_path_norm(ctx):
        return None
    return match.group("literal")


def _parse_pinned_utc(value):
    """-> the ``datetime`` a stamp in the ONE pinned notation denotes, or ``None`` (O141).

    ``YYYY-MM-DDTHH:MM:SSZ``: whole seconds, uppercase ``Z``, no fractional part, no offset.
    ONE notation, for the enable literal's two stamps AND for every ``recordedUtc`` this
    hook reads off a receipt -- which is what makes them comparable at all.  THREE
    fail-closed readings, recorded because none is forced by the words "an ISO-8601 UTC
    stamp":

      * the trailing ``Z`` is REQUIRED and an OFFSET is not it.  ``+00:00`` denotes the same
        instant, and a hook that accepted it would also have to decide what ``-05:00`` and a
        bare ``2026-09-06T12:00:00`` meant.  Normalising is the hook guessing; refusing is
        the hook asking for the one notation the register names.
      * the ``Z`` is UPPERCASE, like the digest is lowercase: the register pins literals, and
        a value that has to be case-folded before it matches is not the pinned literal.
      * a FRACTIONAL part is refused, which the sixth commit accepted in the literal.  A
        fraction is exactly what made two receipt stamps order wrongly as bytes (O141), and
        one notation is cheaper to enforce than an ordering that must be right about every
        notation it admits.

    The regex settles the NOTATION; ``strptime`` settles the CALENDAR, so
    ``2026-02-30T00:00:00Z`` -- right shape, not a date -- is refused too.  No current time
    is read: the question is whether the stamp parses, not when it is.  The RETURN VALUE is
    the parsed instant, precisely so the one site that ORDERS receipts orders by THAT.
    """
    if not isinstance(value, str):
        return None
    if _PINNED_UTC_RX.match(value) is None:
        return None
    try:
        return datetime.datetime.strptime(value, _PINNED_UTC_FORMAT)
    except ValueError:
        return None


def _sha256_of(path):
    """-> the LOWERCASE hex sha256 of the file's BYTES, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except Exception:
        return None


def _enable_literal_ok(ctx, literal, newest_control):
    """S112 -- THE LITERAL IS VALIDATED SEMANTICALLY, NOT FOR PRESENCE.

    ``newest_control`` is the basename ``_kill_switch_receipts_ok`` ALREADY selected on the
    call immediately before this one: the newest valid ``execution-control-*.json`` by the
    PARSED value of its ``recordedUtc`` (O141).  It is passed in rather than recomputed so
    there is exactly ONE answer to "which receipt is newest" in this hook -- the same one
    that decided the exception was open at all.

    The arms are ordered presence -> state -> stamps -> receipt name -> digest, and each
    returns the FIRST failure, so a near-miss differing from the canonical literal in one
    field is attributable to that field and the falsifier rows can each vary one variable.
    """
    try:
        document = json.loads(literal)
    except Exception:
        return False, "the enable literal is not parseable JSON"
    if not isinstance(document, dict):
        return False, "the enable literal is not a JSON object"
    for key in ENABLE_LITERAL_KEYS:
        value = document.get(key)
        if not isinstance(value, str) or not value:
            return False, "the enable literal lacks a non-empty string %s" % key
    if document["state"] != ENABLE_LITERAL_STATE:
        return (
            False,
            'the enable literal\'s state is not "%s"' % ENABLE_LITERAL_STATE,
        )
    for key in ENABLE_LITERAL_UTC_KEYS:
        if _parse_pinned_utc(document[key]) is None:
            return (
                False,
                "the enable literal's %s is %r, which is not the ONE pinned notation %s -- "
                "whole seconds, uppercase Z, no fraction, no offset, the SAME notation "
                "every receipt's recordedUtc carries (S112/O141)"
                % (key, document[key], PINNED_UTC_NOTATION),
            )
    named = document[ENABLE_CONTROL_NAME_KEY]
    if named != newest_control:
        return (
            False,
            "the enable literal names %s %r, but the NEWEST valid execution-control "
            "receipt -- the newest by PARSED recordedUtc -- is %r; a path or a different "
            "basename is not it (S112/O141)"
            % (ENABLE_CONTROL_NAME_KEY, named, newest_control),
        )
    digest = _sha256_of(ctx.receipt(newest_control))
    if digest is None:
        return (
            False,
            "the newest execution-control receipt %r could not be read to hash it (S112)"
            % newest_control,
        )
    if document[ENABLE_CONTROL_SHA_KEY] != digest:
        return (
            False,
            "the enable literal's %s is %r, not the lowercase sha256 of %r, which is %r "
            "(S112)"
            % (
                ENABLE_CONTROL_SHA_KEY,
                document[ENABLE_CONTROL_SHA_KEY],
                newest_control,
                digest,
            ),
        )
    return True, ""


def _selected_control_rehash_why(ctx, selected):
    """O158: re-hash the SELECTED receipt's FIXED set in the board root, else why.

    The set walked is the TABLE's for this step -- ``EXECUTION_CONTROL_REQUIRED_HASHES`` --
    and never merely the keys the receipt happens to carry.  By the time this runs the two
    are equal (S120 refused any receipt whose key set differs), and the distinction is
    load-bearing anyway: it is the register's wording, and it keeps this arm honest if the
    key-set arm were ever loosened.  A file of the set ABSENT from the board root is a
    refusal naming the path; so is a digest that differs from the receipt's, naming the
    path and both digests.  The receipt is RE-READ here rather than carried from the
    selection: it was validated a moment ago, and a read that fails now is a refusal, never
    a pass.  Paths are board-relative in the plan's forward-slash form and are joined to
    the board root, never to the hook's cwd or its own worktree -- $R is the tree the loop
    will run from, and that is the tree the receipt has to match.
    """
    document = read_json(ctx.receipt(selected))
    if not isinstance(document, dict) or not isinstance(
        document.get(CONTROL_HASHES_KEY), dict
    ):
        return (
            "the SELECTED execution-control receipt %s could not be re-read to re-hash its "
            "fixed set (O158)" % selected
        )
    hashes = document[CONTROL_HASHES_KEY]
    required = EXECUTION_CONTROL_REQUIRED_HASHES.get(selected)
    if required is None:
        return (
            "the SELECTED execution-control receipt %s has no row in the fixed hash-set "
            "table (O158)" % selected
        )
    for relative in sorted(required):
        path = os.path.join(ctx.board_root_raw, relative.replace("/", os.sep))
        if not os.path.isfile(path):
            return (
                "the SELECTED execution-control receipt %s hashes %s, but that file is "
                "ABSENT from the board root -- 0.2 re-hashes the step's FIXED set, never "
                "merely the keys present (O158)" % (selected, relative)
            )
        digest = _sha256_of(path)
        if digest is None:
            return (
                "the SELECTED execution-control receipt %s hashes %s, but that file could "
                "not be read to re-hash it (O158)" % (selected, relative)
            )
        recorded = hashes.get(relative)
        if digest != recorded:
            return (
                "the SELECTED execution-control receipt %s records %s at sha256 %r, but in "
                "the board root it hashes to %r -- the board has DRIFTED from the receipt "
                "the enable would be taken against (O158)"
                % (selected, relative, recorded, digest)
            )
    return ""


def _enable_act_why(ctx):
    """-> ``(canonical, why)``: is this shell input the canonical enable, and if so why is it refused.

    ``canonical`` False means "some other shape, judge it generically", and ``why`` is then
    ``""``.  ``canonical`` True with ``why`` empty is the enable, allowed; a non-empty
    ``why`` is the FIRST condition that failed, in this order -- S98 (the receipt is already
    present) first, because a spent authorization settles the input before anything else is
    read; then the receipt set; then the venue; then the O158 re-hash; then the literal.
    ONE validation, TWO callers: ``_enable_act_allowed`` raises ``why`` as the enable's
    refusal, and the S125 pre-flight artifact act quotes it, because the command the
    artifact records must be the compound the enable act would accept RIGHT NOW against the
    same six receipts -- a second implementation would be a second answer.

    S105/O126 -- THE ENABLE ACT IS VENUE-BOUND, and the venue is checked AFTER the receipt
    state and BEFORE the literal.  After, because S98's answer is a standing fact about the
    board that is true at every venue and must be reported as such: an operator holding a
    spent authorization needs to be told the authorization is spent, not that they are in
    the wrong directory.  Before the literal, because a lane may not be told that its
    literal was well-formed.  Without this test the exception is not venue-bound at all: a
    worktree lane's hook evaluates the SAME absolute board paths and would admit the
    compound the moment the six receipts exist -- before the hub has verified $R or
    installed the task (S105).

    O158 -- THE SELECTED RECEIPT'S FIXED SET IS RE-HASHED IN THE BOARD ROOT, after the venue
    and before the literal.  After the venue, because the refusal names which board file
    drifted and to what, which is the board-rooted actor's to see and not a lane's; before
    the literal, because a literal naming a receipt the board no longer matches is a receipt
    of a board that no longer exists, and the operator should be told about the drift, not
    about the digest of a file whose own digests are stale.

    S112 -- THE LITERAL IS VALIDATED SEMANTICALLY, and it stays LAST for the same two
    reasons, now load-bearing in a third way: the digest arm's refusal line names the
    EXPECTED digest, which only the board-rooted actor may see, and only the venue test
    ahead of it guarantees that.  ``_kill_switch_receipts_ok`` hands its own selection down
    as ``newest_control``, so the receipt the literal must name is the receipt the receipt
    arm just accepted.
    """
    literal = _canonical_enable_literal(ctx)
    if literal is None:
        return False, ""
    ok, why, newest_control = _kill_switch_receipts_ok(ctx)
    if not ok:
        return True, why
    if not ctx.at_board_venue:
        return True, (
            "it is the BOARD-ROOTED actor's act and "
            "--project-dir is %s, not %s -- a worktree value or a missing/empty argument "
            "means exception (i) does not apply, because a lane's hook evaluates the same "
            "absolute board paths (S105/O126)"
            % (ctx.project_dir or "<absent>", ctx.board_root)
        )
    why = _selected_control_rehash_why(ctx, newest_control)
    if why:
        return True, why
    ok, why = _enable_literal_ok(ctx, literal, newest_control)
    if not ok:
        return True, why
    return True, ""


def _enable_act_allowed(ctx):
    """-> True when this input IS the canonical enable and every condition holds.

    False means "some other shape, judge it generically".  Raises Deny when it IS the
    canonical act and a condition failed -- the ``why`` of ``_enable_act_why`` above, in
    its order, S98 first.
    """
    canonical, why = _enable_act_why(ctx)
    if not canonical:
        return False
    if why:
        raise Deny("NA-2", "the canonical 0.2 enable is refused: %s" % why)
    return True


def _refuse_marker_delete_outside_the_act(ctx, acts):
    """S99/O118: deleting or moving the marker in ANY non-canonical input is refused.

    Runs BEFORE the generic per-path attribution so the refusal never depends on WHICH
    protected token the scan happens to reach first -- a delete-first compound and a
    receipt-first compound must give the same answer, and generic attribution must never
    be what admits or refuses the enable (O118).
    """
    if "delete" not in acts and "move" not in acts:
        return
    marker = _marker_path_norm(ctx)
    names_marker = False
    for token in tokens(ctx.command):
        path_norm = norm(token)
        if path_norm == marker or _carve_tag(ctx, path_norm) == "killswitch":
            names_marker = True
            break
    if not names_marker:
        return
    verb = "deleting" if "delete" in acts else "moving"
    receipt = _enable_receipt_path_norm(ctx)
    if any(norm(token) == receipt for token in tokens(ctx.command)):
        # S101 and O128 are named here as well as S99/O118 because BOTH superseded shapes
        # -- the rev-18 compound and the rev-19 compound, each of which WAS the canonical
        # form one revision ago -- land on exactly this line, and an operator holding one of
        # them needs to be told which degree of freedom closed rather than being left to
        # re-derive it.  The line names every arm of the current shape; which one a given
        # near-miss dropped is one diff away, and a near-miss is one line to fix.
        raise Deny(
            "NA-2",
            "%s %s outside the canonical enable is never authorized; naming receipts/%s "
            "in a non-canonical shape does not make this the dedicated act (S99/O118), and "
            "the canonical shape is the FAIL-CLOSED one: it OPENS with "
            "$ErrorActionPreference = 'Stop', then -ErrorAction Stop on both acts with the "
            "read-back throw between them (S101)%s"
            % (
                verb,
                KILL_SWITCH_MARKER_NAME,
                KILL_SWITCH_ENABLE_RECEIPT,
                _enable_preference_gap(ctx.command),
            ),
        )
    raise Deny(
        "NA-2",
        "%s %s outside the canonical enable is never authorized (S99)"
        % (verb, KILL_SWITCH_MARKER_NAME),
    )


def _archive_release_ok(ctx, path_norm, new_text):
    """Exception (ii): a byte-identical archive copy exists and the stub names its sha256."""
    if new_text is None:
        return False
    import hashlib

    target = None
    for candidate in (ctx.path,):
        if candidate and os.path.isfile(candidate):
            target = candidate
    if target is None:
        return False
    try:
        with open(target, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except Exception:
        return False
    archive = os.path.join(ctx.board_root_raw, ".claude-state", "continuity", "archive")
    matched = False
    for root, _dirs, files in os.walk(archive):
        for name in files:
            try:
                with open(os.path.join(root, name), "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() == digest:
                        matched = True
                        break
            except Exception:
                continue
        if matched:
            break
    if not matched:
        return False
    return digest.lower() in new_text.lower()


def _file_shrinks(ctx):
    """Does this Write/Edit/NotebookEdit make an EXISTING target shorter?"""
    if ctx.tool == "Edit":
        if ctx.old_text is None or ctx.new_text is None:
            return True  # fail closed: an unreadable edit cannot be proven non-shrinking
        return len(ctx.new_text) < len(ctx.old_text)
    if ctx.new_text is None:
        return True
    if not ctx.path or not os.path.isfile(ctx.path):
        return False  # a create is create-or-extend, which the carve-out allows
    try:
        existing = os.path.getsize(ctx.path)
    except Exception:
        return True
    return len(ctx.new_text.encode("utf-8")) < existing


RECEIPTS_DIR_SEG = "receipts"
FIXED_RECEIPT_BASENAMES = frozenset(name.lower() for name in KILL_SWITCH_RECEIPTS)


def _create_is_admissible(ctx, path_norm):
    """HOOK-FALSE-POSITIVE-1: is this a CREATE the generic NA-2 arm has no cause to refuse?

    Only a ``Write`` (an ``Edit`` names text it claims already exists, and a
    ``NotebookEdit`` amends a document), only when the payload carries its new content, and
    only when nothing is on disk at the target -- so the refusal this replaces, which is
    about OVERWRITING, still fires for every real overwrite.

    TWO NAME CLASSES ARE HELD BACK, and they keep exactly the behaviour they had before
    this delta rather than joining the new allowance: anything under a ``receipts/``
    directory, and any basename that is an ``execution-control-*`` chain name or one of the
    five FIXED gate receipts.  Those are the files whose PRESENCE spends the one-shot 0.2
    authorization and whose set the enable act ENUMERATES, so a create under them is never
    the harmless act "nothing was there" makes it look like.  ``$D/receipts/**`` already
    reaches the receipts carve-out one branch up and never arrives here at all; this guard
    is what stops the new allowance from admitting a receipt name under any OTHER protected
    directory (`.claude-state/coordination/receipts/`, `.claude-state/closeout/receipts/`).
    """
    if ctx.tool != "Write" or ctx.new_text is None:
        return False
    if not ctx.path or os.path.isfile(ctx.path):
        return False
    if has_seg(path_norm, RECEIPTS_DIR_SEG):
        return False
    base = path_norm.rsplit("/", 1)[-1]
    if base.startswith(EXECUTION_CONTROL_PREFIX):
        return False
    if base in FIXED_RECEIPT_BASENAMES:
        return False
    return True


def _na2_decide(ctx, path_norm, acts, source):
    """One protected path, one act set.  Raises Deny, or returns for ALLOW."""
    tag = _carve_tag(ctx, path_norm)

    if tag == "killswitch":
        if "delete" in acts or "move" in acts:
            # Unreachable for shell inputs -- ``_refuse_marker_delete_outside_the_act``
            # already settled them, and the canonical act returned before attribution.
            # Kept as the fail-closed floor: no path reaches this arm and is allowed.
            raise Deny(
                "NA-2",
                "%s %s outside the canonical enable is never authorized (S99)"
                % ("deleting" if "delete" in acts else "moving", KILL_SWITCH_MARKER_NAME),
            )
        # Exception (iii): CREATING the kill switch is ALWAYS allowed, any tool, any verb.
        return

    if tag is not None:
        if "delete" in acts:
            raise Deny("NA-2", "deleting %s is denied on the carve-out paths" % path_norm)
        if "move" in acts:
            raise Deny("NA-2", "moving %s is denied on the carve-out paths" % path_norm)
        if source == "shell":
            # O125: THE CARVE-OUT IS A PATH PERMISSION, NOT A TOOL PERMISSION.  The hook
            # sees a command's TEXT, never its outcome, so a shell truncating write cannot
            # be proven non-shrinking even when it is a create -- the same path reached by
            # `Write`/`Edit` carries its new content in the payload and CAN be.  So a shell
            # `Set-Content` create of `receipts/x.json` is DENY while the `Write` create of
            # that path is ALLOW; receipt writes after 0.1 use the file tools or an append
            # (`Add-Content`, `>>`).  0.2's canonical compound is the one shell write these
            # paths admit, and only because the dedicated act is evaluated BEFORE this rule
            # -- its `Set-Content` half is denied here too, not merely its `Remove-Item`.
            if "truncate" in acts:
                raise Deny(
                    "NA-2",
                    "a truncating write to %s cannot be proven non-shrinking" % path_norm,
                )
            return
        if _file_shrinks(ctx):
            if tag in ("resume", "seat") and _archive_release_ok(ctx, path_norm, ctx.new_text):
                return
            raise Deny(
                "NA-2",
                "shrinking overwrite of %s (archive with a stub instead)" % path_norm,
            )
        return

    # ---------------------------------------------- NA-2 exception (iv): the VENUE (O124)
    #
    # Reached only for a path that is NOT on a carve-out, so the venue can never loosen the
    # shrink guard or the kill switch: those returned above.  Once the hook is live in the
    # hub's session every route to amending a manifest surface is denied (hub-measured:
    # `Write`, `Edit` and shell `Set-Content` of the plan all exit 2), yet re-ratification
    # (plan 1.3) and 0.2's interruption recovery both require exactly that.  What separates
    # the amendment actor from a lane is not the TOOL and not the LENGTH -- both were tried
    # and both are forgeable by a lane -- it is the VENUE, and since O126 the venue rides on
    # ARGV: `--project-dir` is substituted by Claude Code from `${CLAUDE_PROJECT_DIR}` in
    # the registered command, no lane is ever rooted at the board (the wrapper refuses
    # `workdir-is-board-root`), and a lane's tool input cannot set a command-line argument.
    # The environment variable this argument is substituted FROM is not read here: it was
    # measured ABSENT from real hook processes, which is why the rev-19 mechanism never
    # fired.  Delete/move stays DENIED at every venue: the guard on an amendment is that
    # plan 1.3 re-hashes and re-ratifies it, and there is nothing to re-hash after a delete.
    surface = _manifest_surface(ctx, path_norm)
    if surface is not None:
        if "delete" in acts or "move" in acts:
            raise Deny(
                "NA-2",
                "%s the ratified manifest surface %s is denied at EVERY venue; exception "
                "(iv) admits writes only, because an amendment is re-hashed and "
                "re-ratified and a deletion cannot be (O124)"
                % ("deleting" if "delete" in acts else "moving", path_norm),
            )
        if ctx.at_board_venue:
            return  # exception (iv): the board-rooted amendment actor, any tool, any length
        raise Deny(
            "NA-2",
            "writing the ratified manifest surface %s needs NA-2 exception (iv), and this "
            "session is not the board-rooted actor: --project-dir is %s, not %s (absent, "
            "empty or a worktree value means the exception does not apply -- O124/O126)"
            % (path_norm, ctx.project_dir or "<absent>", ctx.board_root),
        )

    if source == "shell":
        if acts:
            raise Deny(
                "NA-2",
                "%s of ledger/pen/receipt/evidence content at %s"
                % ("/".join(sorted(acts)), path_norm),
            )
        return
    # HOOK-FALSE-POSITIVE-1: OVERWRITING is what this arm names and what it may refuse.  A
    # `Write` whose target does NOT EXIST overwrites nothing -- it is the same
    # create-or-extend the carve-outs already allow, and `_file_shrinks` already reads it
    # that way one branch up.  Denying it cost the board five orchestrator refusals in two
    # days (measured 2026-09-07/08), each on a NEW file under
    # `.claude-state/coordination/dual-lane/` that nothing existed at.
    if _create_is_admissible(ctx, path_norm):
        return
    raise Deny("NA-2", "overwriting ledger/pen/receipt/evidence content at %s" % path_norm)


# ------------------------------ NA-2, S125: the PRE-FLIGHT ARTIFACT, the SECOND dedicated act
#
# PRE-FLIGHT (S104): before typing the enable the hub writes the EXACT hook-stdin JSON of the
# final board-path compound -- `{"tool_name":"PowerShell","tool_input":{"command":"<the
# compound>"}}` -- to `$D\receipts\0.2-enable-preflight-input.json` with the `Write` tool,
# records its sha256, and runs the LIVE board's hook on that file under MLV_HOOK_DRYRUN=1, so
# the shape boundary the enable act draws is proven BEFORE the one-shot act rather than
# discovered by it.  That Write carries content naming BOTH enable paths, and the register
# says any other input naming both paths is DENIED -- so on the tenth commit the hub measured
# a TEXT/CODE GAP: the Write was ALLOWED, but by the receipts carve-out (a create under
# `$D\receipts\**`), not by any rule about what it carried; and the same content written to
# `notes.json`, under `fleet-runs/`, and from a worktree venue was ALLOWED too (measured
# again on this repo before this delta: four ALLOWs).  S125 pins the decision BY RULE, as the
# second dedicated act:
#
#   * a `Write` -- that tool name exactly; an `Edit` or `NotebookEdit` of the path is refused
#     as a near-miss of the act, never judged as a carve-out extend -- whose normalised path
#     is EXACTLY the artifact's (derived from MLV_BOARD_ROOT like every enable path, so a
#     scratch root can never match), is ALLOWED only when ALL hold, tested in this order:
#     the venue is the board root (the enable act's own test, S105/O126, for the same
#     reason -- a lane's hook evaluates the same absolute board paths); the target does NOT
#     exist on disk (the act CREATES the artifact; a stale one is the hub's to recover); and
#     the content parses as exactly ONE JSON object whose `tool_name` is `PowerShell` and
#     whose `tool_input.command` is the canonical enable compound the enable act would
#     accept RIGHT NOW -- `_enable_act_why` on an inner context, the SAME validation (the
#     semantic literal, the receipt schemas, the chain selection, the O158 re-hash), never a
#     second one.  It writes evidence only and authorizes no delete: nothing about the
#     marker changes because this act was allowed.
#   * it is evaluated BEFORE generic content attribution and after the venue test, exactly
#     as the enable act is evaluated before generic per-path attribution, and for the same
#     reason: attributing the content's verbs to the paths it names would deny the artifact
#     itself.
#   * GENERIC CONTENT ATTRIBUTION for `Write`/`Edit`/`NotebookEdit` then follows: content
#     that names BOTH enable paths, or carries the marker under a delete verb, written to
#     ANY other path -- `notes.json`, a fleet-run receipt, a checkpoint, a relative-path copy
#     of the artifact -- is DENIED, naming the path, BEFORE the carve-outs are consulted.  A
#     carve-out is a permission on a PATH; this is a rule about CONTENT, and the carve-out
#     keeps every OTHER create-or-extend under `$D\receipts\**`.
#   * MLV_HOOK_DRYRUN=1 prints the decision for this act exactly as for the enable act: the
#     hook is DECISION-ONLY here as everywhere -- it reads the receipts and the board and
#     writes nothing -- which is what makes the pre-flight probe safe against the live board.
#
# FAIL-CLOSED READINGS (recorded, eleventh delta):
#   * "names a path" is a SUBSTRING test on the payload after `norm`'s reading -- backslashes
#     to slashes, runs of slashes collapsed, case folded -- so the JSON-escaped `C:\\board`
#     the artifact actually carries, a bare `C:/board` and a mixed form are ONE path; and
#     when the payload carries a JSON `\u` escape and parses as JSON, its decoded strings are
#     scanned too, so `\u005c` is not a way around a text matcher.  The EXACT board paths
#     are matched -- the register's "both paths" are the board-absolute ones the compound
#     carries -- and only the NEW content of a payload (`content`, `new_string`,
#     `new_source`): an `Edit` whose `old_string` names both paths is removing them.
#   * The limit is unchanged and restated: an interpreter one-liner, or content encoded in a
#     form no text matcher decodes, is invisible here; the layers behind this rule are the
#     shell arm's S99/O118 refusal of any command that deletes the marker outside the act,
#     and sol's review of every PR diff.


def _preflight_artifact_path_norm(ctx):
    return norm(ctx.receipt(KILL_SWITCH_PREFLIGHT_ARTIFACT))


def _preflight_refused(reason):
    """The S125 refusal, naming the file and the one reason."""
    return Deny(
        "NA-2",
        "the PRE-FLIGHT ARTIFACT act -- a Write of exactly receipts/%s -- is refused: %s "
        "(S125)" % (KILL_SWITCH_PREFLIGHT_ARTIFACT, reason),
    )


def _preflight_artifact_act_allowed(ctx):
    """S125: -> True when this file-tool input IS the pre-flight artifact act and every condition holds.

    False means "the path is not the artifact's, judge it generically".  Raises Deny when the
    path IS the artifact's and a condition failed, in the order the register lists them --
    the tool, the venue, the target's absence, the content's shape, its inner tool name, and
    its command's canonical shape and semantic validity -- each refusal naming the file and
    the one thing that failed.  The command is validated by ``_enable_act_why`` on an inner
    PowerShell context at the same venue: the compound the artifact records must be the
    compound the enable act would accept against the CURRENT six receipts, and one
    validation with two callers is what makes that so.  Nothing is performed: the inner
    context is decided and discarded.
    """
    if ctx.tool not in FILE_TOOLS or not ctx.path_norm:
        return False
    if ctx.path_norm != _preflight_artifact_path_norm(ctx):
        return False
    if ctx.tool != PREFLIGHT_TOOL:
        raise _preflight_refused("this input is %s, not %s" % (ctx.tool, PREFLIGHT_TOOL))
    if not ctx.at_board_venue:
        raise _preflight_refused(
            "it is the BOARD-ROOTED actor's act and --project-dir is %s, not %s -- a "
            "worktree value or a missing/empty argument means the act does not apply, "
            "because a lane's hook evaluates the same absolute board paths (S105/O126)"
            % (ctx.project_dir or "<absent>", ctx.board_root)
        )
    if os.path.exists(ctx.receipt(KILL_SWITCH_PREFLIGHT_ARTIFACT)):
        raise _preflight_refused(
            "the target already EXISTS on disk, and the act CREATES it -- a pre-flight "
            "artifact is written once, before the enable, and this act never overwrites one"
        )
    if ctx.new_text is None:
        raise _preflight_refused("it carries no content")
    try:
        document = json.loads(ctx.new_text)
    except ValueError as error:
        raise _preflight_refused(
            "its content does not parse as exactly ONE JSON object (%s)" % error
        )
    if not isinstance(document, dict):
        raise _preflight_refused("its content is not a JSON object")
    if document.get("tool_name") != PREFLIGHT_INNER_TOOL:
        raise _preflight_refused(
            "its tool_name is %r, not %r" % (document.get("tool_name"), PREFLIGHT_INNER_TOOL)
        )
    inner_input = document.get("tool_input")
    command = inner_input.get("command") if isinstance(inner_input, dict) else None
    if not isinstance(command, str) or not command:
        raise _preflight_refused("its tool_input carries no non-empty string command")
    canonical, why = _enable_act_why(
        Ctx(PREFLIGHT_INNER_TOOL, {"command": command}, ctx.project_dir_raw)
    )
    if not canonical:
        raise _preflight_refused(
            "its command is not the canonical enable compound%s"
            % _enable_preference_gap(command)
        )
    if why:
        raise _preflight_refused(
            "its command is the canonical shape but is not semantically valid for the "
            "CURRENT six receipts on disk: %s" % why
        )
    return True


_JSON_UNICODE_ESCAPE = "\\u"


def _content_scan_text(text):
    """The NEW content of a file-tool payload, read the way ``norm`` reads a path.

    Backslashes become slashes, runs of slashes collapse and the case folds, so the
    JSON-escaped ``C:\\\\board`` the pre-flight artifact carries, a bare ``C:/board`` and a
    mixed form all read as the ONE normalised path a rule compares against.  When the text
    carries a JSON ``\\u`` escape and parses as JSON, its decoded strings (keys and values,
    at any depth) are appended to the scan, so an escape is not a way around the matcher.
    """
    parts = [text]
    if _JSON_UNICODE_ESCAPE in text:
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        stack = [document]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    joined = "\n".join(parts).replace("\\", "/")
    return re.sub(r"/{2,}", "/", joined).lower()


def _content_names(scan_text, path_norm):
    """Does the normalised payload text contain this normalised board path?"""
    return path_norm.lstrip("/") in scan_text


def _refuse_enable_content_outside_the_act(ctx):
    """S125/S99/O118: the enable's content, written by a file tool to ANY other path, is refused.

    Two arms, both about what the payload CARRIES and neither about where it goes: content
    naming BOTH the enable receipt and the marker (the pre-flight artifact's content, or the
    compound itself); and content carrying the marker under a delete or move verb (a script
    that would disarm the kill switch, saved for later).  The pre-flight artifact act
    returned before this runs, so the ONE file-tool input that may carry the compound has
    already been judged; everything else that carries it is refused HERE, naming the path,
    before the path guard and its carve-outs -- a carve-out is a permission on a PATH, and
    the hub measured on the tenth commit that the receipts carve-out admitted exactly this
    content on exactly this act.  Cheap on the common path: a payload that carries neither
    the marker's leaf nor a ``\\u`` escape returns after one case fold.
    """
    text = ctx.new_text
    if text is None:
        return
    if KILL_SWITCH_LEAF not in text.lower() and _JSON_UNICODE_ESCAPE not in text:
        return
    scan = _content_scan_text(text)
    if not _content_names(scan, _marker_path_norm(ctx)):
        return
    target = ctx.path_norm or "<no path>"
    if _content_names(scan, _enable_receipt_path_norm(ctx)):
        raise Deny(
            "NA-2",
            "writing content that names BOTH the enable receipt receipts/%s and the marker "
            "%s to %s is denied: only the PRE-FLIGHT ARTIFACT act -- a Write of exactly "
            "receipts/%s at the board venue -- may carry that content, and generic "
            "attribution never admits the enable (S125/S99/O118)"
            % (
                KILL_SWITCH_ENABLE_RECEIPT,
                KILL_SWITCH_MARKER_NAME,
                target,
                KILL_SWITCH_PREFLIGHT_ARTIFACT,
            ),
        )
    acts = shell_acts(text)
    if "delete" in acts or "move" in acts:
        raise Deny(
            "NA-2",
            "writing content that carries a %s verb beside the marker %s to %s is denied: "
            "the delete verb naming the marker in any input other than the canonical "
            "enable is never authorized (S125/S99)"
            % ("delete" if "delete" in acts else "move", KILL_SWITCH_MARKER_NAME, target),
        )


def rule_na2(ctx):
    if ctx.tool in SHELL_TOOLS:
        # The dedicated act is decided BEFORE anything is attributed (S99/O118).
        if _enable_act_allowed(ctx):
            return
        acts = shell_acts(ctx.command)
        if not acts:
            return
        _refuse_marker_delete_outside_the_act(ctx, acts)
        # HOOK-FALSE-POSITIVE-1: "truncate" is attributed to the TARGET of the redirect or
        # the cmdlet, never to every protected token that happens to share the command
        # line.  Delete and move keep the whole-command attribution they have always had:
        # `rm -rf A B` really does name both, and a delete verb's operands are the tokens.
        # A truncation, by contrast, has exactly one destination per redirect, and reading
        # a protected file BESIDE one is the commonest read-only act on this board.
        positional = acts - {"truncate"}
        targets = (
            frozenset(norm(dest) for dest in truncating_destinations(ctx.command))
            if "truncate" in acts
            else frozenset()
        )
        for token in tokens(ctx.command):
            path_norm = norm(token)
            if not _is_na2_protected(path_norm):
                continue
            token_acts = set(positional)
            if path_norm in targets:
                token_acts.add("truncate")
            if not token_acts:
                continue
            _na2_decide(ctx, path_norm, token_acts, "shell")
        return
    # S125: the SECOND dedicated act is decided BEFORE generic content attribution, and
    # content attribution BEFORE the path guard and its carve-outs -- a carve-out is a
    # permission on a PATH, and this is a rule about CONTENT.
    if _preflight_artifact_act_allowed(ctx):
        return
    _refuse_enable_content_outside_the_act(ctx)
    if not ctx.path_norm or not _is_na2_protected(ctx.path_norm):
        return
    _na2_decide(ctx, ctx.path_norm, {"write"}, "file")


# ------------------------------------------------------------------------- NA-3

_TOKEN_PREFIXES = r"(?:ANTHROPIC_|OPENAI_|CLAUDE_CODE_)"
_NA3_RULES = (
    (
        re.compile(r"(?:^|[\s;&|`(])" + _TOKEN_PREFIXES + r"\w*\s*=", re.I),
        "assignment of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (
        re.compile(r"\bexport\s+" + _TOKEN_PREFIXES, re.I),
        "export of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (
        re.compile(r"\$env:" + _TOKEN_PREFIXES + r"\w*\s*=", re.I),
        "$env: assignment of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (
        re.compile(r"\bset\s+" + _TOKEN_PREFIXES + r"\w*\s*=", re.I),
        "`set` assignment of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (
        re.compile(r"\bsetx(?:\.exe)?\s+" + _TOKEN_PREFIXES, re.I),
        "`setx` of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (
        re.compile(
            r"setenvironmentvariable\s*\(\s*['\"]" + _TOKEN_PREFIXES, re.I
        ),
        "SetEnvironmentVariable of a name starting ANTHROPIC_/OPENAI_/CLAUDE_CODE_",
    ),
    (re.compile(r"\bcodex\b[^\n;|&]*\blogin\b", re.I), "`codex login` is the owner's own keystrokes"),
)

# ---- O129: PERSISTENT-scope assignment of the six names the hook itself is steered by ----
#
# The prefix rule above is about CREDENTIALS.  This one is about the hook's own inputs, and
# the act it denies is narrower and sharper: a PERSISTENT write (`setx`, or
# `[Environment]::SetEnvironmentVariable(..., 'User'|'Machine')`) of `CLAUDE_PROJECT_DIR`,
# `MLV_BOARD_ROOT`, `MLV_LANE_PROMPT`, `MLV_REQUIRED_CHECKS_SNAPSHOT` or
# `MLV_CLIP_CACHE_ROOT`.  Those five decide, respectively, the registered venue argument's
# source, the board root every path in NA-2/NA-7/NA-10 is judged against, the clip
# authorization NA-4 reads, the required-checks snapshot NA-9 fails closed without, and the
# cache prefix NA-4 guards.  THE RULE IS THE LIST: a new
# input that any rule reads is added here in the same commit that introduces it -- and the
# recorded reason is a steering root that once shipped ahead of this list and could be
# `setx`-ed to `C:\`, which is one of the four bypasses that got that root removed entirely.
# A persistent write is INHERITED BY EVERY LATER HOOK PROCESS on
# this machine: it does not break a rule, it MOVES the ground the rules stand on, and it
# survives the session that made it.  A plain in-process `$env:MLV_BOARD_ROOT = ...` is NOT
# this act -- it dies with its shell, reaches no hook process, and the test carries an
# explicit ALLOW control for it so the distinction cannot quietly widen into a ban on
# reading or setting these names at all.
#
# FAIL-CLOSED READINGS (recorded, 0.05 fifth review delta):
#   * NAME and SCOPE are matched case-insensitively.  Windows environment variable names are
#     case-insensitive and `[EnvironmentVariableTarget]` parses case-insensitively, so
#     `setx mlv_board_root` is the same act as `setx MLV_BOARD_ROOT`.
#   * The scope is looked for ANYWHERE in the statement after the name, as a quoted literal
#     OR as `[EnvironmentVariableTarget]::User|Machine`, rather than as the strict third
#     positional argument.  Parsing PowerShell argument positions from text is exactly the
#     kind of near-parse that fails open on a line-continuation or a splatted value.
#   * `setx` has NO process-scope form at all -- every `setx` is persistent -- so the name
#     alone is enough there.
_NA3_PERSISTENT_NAMES = (
    r"(?:CLAUDE_PROJECT_DIR|MLV_BOARD_ROOT|MLV_LANE_PROMPT|MLV_REQUIRED_CHECKS_SNAPSHOT"
    r"|MLV_CLIP_CACHE_ROOT)"
)
_NA3_PERSISTENT_SCOPE = (
    r"(?:['\"](?:User|Machine)['\"]|\[(?:System\.)?EnvironmentVariableTarget\]\s*::\s*(?:User|Machine))"
)
_NA3_PERSISTENT_RULES = (
    (
        re.compile(r"\bsetx(?:\.exe)?\s+['\"]?" + _NA3_PERSISTENT_NAMES + r"\b", re.I),
        "`setx` writes a PERSISTENT %s, inherited by every later hook process (O129)",
    ),
    (
        re.compile(
            r"setenvironmentvariable\s*\(\s*['\"]"
            + _NA3_PERSISTENT_NAMES
            + r"['\"][^\n;|&]*"
            + _NA3_PERSISTENT_SCOPE,
            re.I,
        ),
        "a User/Machine-scope SetEnvironmentVariable writes a PERSISTENT %s, inherited by "
        "every later hook process (O129)",
    ),
)
_NA3_PERSISTENT_NAME_RX = re.compile(_NA3_PERSISTENT_NAMES, re.I)

# ---- the `claude auth ...` arm, bounded by the REGISTER and not by the card's phrase ----
#
# ``never-authorized.json`` (schema v21) row NA-3 OPENS, VERBATIM:
#
#     claude auth login|logout, codex login, or assignment (X=, export, $env:, set, setx)
#     of any variable starting ANTHROPIC_, OPENAI_, CLAUDE_CODE_
#
# (the row has since grown a PERSISTENT-scope clause -- handled by the O129 arm above -- and
# the explicit `status` allowance; this is its OPENING clause, quoted for the two verbs)
#
# so the acts the register never authorizes are `login` and `logout`.  The card's prose
# says only "claude auth", and this hook shipped a `\bclaude\b...\bauth\b` catch-all that
# also denied `claude auth status` -- a READ-ONLY identity check that MUST keep running.
# The board's account-rotation procedure settles CLI-versus-desktop identity with exactly
# `claude auth status --json`, and from plan step 0.1 onward the hub session that runs it
# is itself under this hook; denying it would break the one procedure that detects an
# account drift, while stopping no never-authorized act.
#
# FAIL-CLOSED READING OF THE GAP (recorded, 0.05 review).  The register names two verbs;
# this arm admits exactly ONE -- `status`, alone or with any flags.  EVERY other
# `claude ... auth ...` form is DENIED: `auth` with no subcommand, and any verb this hook
# does not recognise.  The alternative -- a bare `login|logout` denylist that matches the
# register literally -- fails OPEN on the next auth verb the CLI grows, and NA-3 is a
# fail-closed rule.  The cost of this reading is a false DENY on a future read-only verb,
# which is visible and one line to fix; the cost of the other is a silent ALLOW.
_NA3_CLAUDE_AUTH_RX = re.compile(r"\bclaude\b(?P<rest>[^\n;|&<>]*\bauth\b[^\n;|&<>]*)", re.I)
_NA3_AUTH_SPLIT_RX = re.compile(r"\bauth\b", re.I)
_NA3_LOGIN_RX = re.compile(r"\b(?:login|logout)\b", re.I)
NA3_CLAUDE_AUTH_READONLY_VERB = "status"


def _na3_claude_auth(text):
    """The `claude auth ...` arm.  Returns a one-line DENY reason, or None to ALLOW."""
    for match in _NA3_CLAUDE_AUTH_RX.finditer(text):
        segment = match.group("rest")
        if _NA3_LOGIN_RX.search(segment):
            return "`claude auth login|logout` is the owner's own keystrokes (register NA-3)"
        verbs = []
        for word in _NA3_AUTH_SPLIT_RX.split(segment, 1)[-1].split():
            word = word.strip("\"'")
            if not word or word.startswith("-"):
                continue  # a flag, in any order
            verbs.append(word.lower())
        if verbs == [NA3_CLAUDE_AUTH_READONLY_VERB]:
            continue  # read-only `claude auth status [--json]`: the rotation check
        return (
            "`claude auth` without the one read-only subcommand `status` cannot be proven "
            "read-only (register NA-3 names login|logout; fail-closed on any other verb)"
        )
    return None


def rule_na3(ctx):
    text = ctx.subject
    if ctx.tool not in SHELL_TOOLS:
        return
    for pattern, why in _NA3_RULES:
        if pattern.search(text):
            raise Deny("NA-3", why)
    for pattern, why in _NA3_PERSISTENT_RULES:
        match = pattern.search(text)
        if match:
            named = _NA3_PERSISTENT_NAME_RX.search(match.group(0))
            raise Deny("NA-3", why % (named.group(0).upper() if named else "hook input"))
    why = _na3_claude_auth(text)
    if why:
        raise Deny("NA-3", why)


# ------------------------------------------------------------------------- NA-4

_CLIP_LINE_RX = re.compile(r"^(?:- )?CLIP_OR_NONE:\s*(.*?)\s*$", re.M)
FIXTURE_TAIL = "tests/fixtures/clips"

# NA4-OWNER-CONSENTED-FOOTAGE-1 -- THE OWNER-CONSENTED FOOTAGE RECORD, WHO DECIDED IT, AND
# WHAT IT BINDS.
#
# AUTHORITY (round 2, re-cited; every line hash is sha256 of the raw JSONL line bytes EXCLUDING
# the line terminator).  The CONSENT is the owner's own typed line in the peaceful-rubin
# transcript, line 1739, answering the assistant's question at line 1718.  The hub transcript's
# line 2137 is a DIFFERENT owner line: the directive against manual path steps; it names no
# clip.  The six ids are the HUB's derivation, not the owner's words (``scope_derivation``
# below); M02-1344 is NOT here.  No lane, quorum or adjudication can widen this table.
#
# ROUND 6: NARROW.  Rounds 1-5 used this table for an NA-4 exception (3) that ADMITTED a
# consented path named in agent command text.  Every round found a new command-text bypass,
# because this hook evaluates text that differs from what bash, PowerShell or cmd execute.
# That admission is REMOVED: this hook admits NO consented path from command text, and a
# consented-footage path in command text is DENIED by NA-4 exactly as on master.  Only NA-4
# exceptions (1) tracked fixtures and (2) CLIP_OR_NONE remain.  The table stays as the CONSENT
# RECORD that tracked, id-addressed consumers verify content against.
#
# WHAT IS FROZEN.  Per consented id, per part: the byte length, the lowercase sha256 of the
# content, and ``path_norm_sha256`` -- the sha256 of this hook's own ``norm()`` output for that
# part's path, UTF-8 encoded.  NO PATH is stored.  No rule in this hook reads the table.
#
# LIMITS, stated rather than papered over.  TWO NUMBERINGS MEET IN THIS BLOCK AND ARE KEPT
# APART: the limits below are labelled L1..L6, and the NA-4 exceptions keep their own (1) and
# (2) but are ALWAYS written as "NA-4 exception (n)", never as a bare number.
#
# L1. THE ROUTE THIS CARD PROVIDES for consented
# footage is tracked, id-addressed consumers that verify content against this table with
# tools/gates/verify_consented_footage.py (each part's existence, length and sha256).  None is
# wired yet: the CUDA job (card ATTR3-FOOTAGE-BIND-1) and the #72b delta tooling are not, and
# until then the CUDA job generator refuses every owner-clip id.  THAT IS NOT THE ONLY WAY A
# CONSENTED CLIP CAN BE OPENED TODAY.  NA-4 exception (2) admits the ONE canonical path on the
# card's CLIP_OR_NONE line, and NOTHING EXCLUDES A CONSENTED CLIP'S OWN PATH FROM IT: a
# consented clip named there is opened with NO content check against this table and by no
# id-addressed consumer, exactly as before this card.
#
# L2. THE NARROWING REDUCES
# SCOPE, NOT EXPOSURE.  The interpreter-one-liner residual is UNCHANGED: a text-matching hook
# never sees the path an interpreter one-liner or a script opens, so such a one-liner can open
# any path, consented or not, exactly as before this card.  Removing NA-4 exception (3) removes
# a review surface; it does not close that hole.
#
# L3. Widening needs a change to THIS table,
# which is visible in the diff and VALUE-pinned by tests
# (tools/repo_hygiene/test_mlv_never_authorized.py pins the sha256 of a canonical
# serialisation of the WHOLE table -- ids, per-part length, content sha256 and
# path_norm_sha256, part counts, purposes and citations -- plus each id's part count, so an
# in-place hash swap or an appended part row goes red); the merge path is agent-reviewed and
# CI-test-pinned, NOT an un-mintable owner gate -- the repo has zero required reviews and one
# shared account.  A board-rooted actor can still edit the table transiently: that is the same
# residual as every NA rule at the board venue, bounded by the NA-10 venue gate and the 0.05
# hook-enforced receipt that Invoke-Lane checks.
#
# L4. The purposes are recorded, not enforced.
# L5. NA-4 clip detection does not strip trailing dots or spaces from a clip name outside the
# cache, so such a spelling is not detected as a clip; this predates the card, and a follow-up
# card (NA4-CLIP-NAME-TRIM-1) fixes it.
# L6. NA-4 exceptions (1) and (2) still compare the EXPANDED token, so a hook-read variable a
# child shell reassigns can open a different target; this predates the card, and follow-up card
# NA4-EXC12-LITERAL-TOKENS-1 tracks it.
OWNER_CONSENTED_FOOTAGE = types.MappingProxyType(
    {
        # clip id -> ((length, content sha256, path_norm_sha256), ...) in part order.
        "clips": types.MappingProxyType(
            {
                "M16-1243": (
                    (
                        2221399552,
                        "c8f09e1ea8e6c40f673bf465d2d4adb90ca218bc9f78c6461db5cd22cd3e01b9",
                        "28a106d4cc483f1ffd83d54c0b95536c4e7a21a127808fad7dfa3d8f25c0f552",
                    ),
                    (
                        1174057472,
                        "5dd782178e1658dad5da31808bf3a7c249f7e282682220e4d72b7743dacc163b",
                        "0f2c4cb7c41a4f836e38acfe44a0d7dcd589b0dc8f889986668f05e30a32de7c",
                    ),
                ),
                "M16-1327": (
                    (
                        2654424576,
                        "cfdcd64da9931db94b2b4f938fec1e10b8a742c2e79847975bdd6a986e137a3f",
                        "cba05ef72d33bb8116c21d912938575f173ee5b54975d64e85bc98847a3f17af",
                    ),
                    (
                        1327107584,
                        "c13ed73d60a5767f58bee0a22bbb046f8cf795a8c09511689bbfa6213e770cd7",
                        "a823c17ae653fb2b8c5d7abd6a7027e160c6c5dab481df9c46967cfc0cbafec3",
                    ),
                ),
                "M16-1347": (
                    (
                        2454400512,
                        "763e5a302a8d04e97d84ca7d281d65752529a6ed00bf5a7ff8bbb1e46609934d",
                        "63fbc5a24cc158754bacd0dd2d4be7abdc92053134a603b6f983d60602d5da52",
                    ),
                    (
                        1236876288,
                        "e32c19b7b61619e1ea29c05358a23e863218ba021c4d2e8bdda5d057417d5fb1",
                        "6af3176539897975fb494a9ec370fa476e474660c664e344fa9fd1df1c2d1e1d",
                    ),
                ),
                "M17-1207": (
                    (
                        2367993856,
                        "3bbb5034d8ab5d63a078517af5a3098fff89677be37e78298317a19def043dce",
                        "5fc530e0e0e1a06d34cc702ba48de0a6db3e8018a0045989cb7315581a49574e",
                    ),
                    (
                        1369447936,
                        "3ab34479a80363a693bce3e13782b828b1eccbc38e96dd11ad0eb8c2d919a54e",
                        "cb94f742140e72e46ee029eb40b93cfed9299f00a68c87e3d9fc59b39a60aea3",
                    ),
                ),
                "M15-1320": (
                    (
                        462899712,
                        "b72347ace4f18981a9ea66db9fc2db9413718c99d4818a1846413b86b703ca1e",
                        "81a01b33a298de563aed14851308ce0e706e3287b9413b9e42d8b68bc3dd0873",
                    ),
                    (
                        228077056,
                        "8b7de65f58e9118001e53e1b6490784c0fa2cda0ef108e7eb63c8d62f5c7e999",
                        "bae2d8f11f9b221456feec6d9e699b63cffd9af607bdc6e5bde689d8f628cad6",
                    ),
                ),
                "M16-1210": (
                    (
                        793847808,
                        "679ee976b0150c71ca26f7d8895c775afdca273fa7f907db22cf7c3d58af17cd",
                        "e29feea8e1cd1c5a77ea1efbb4001af0e5d81659b3f25631df9567c89ce290c0",
                    ),
                    (
                        395081728,
                        "d7a31dbb9b2afe717485a4f664619c7d4f2e747c3e13be1d503f93904551af84",
                        "c084dd034f80f90ce3ec26ef25a635fe8192ddcc9bf63ab77e9661013585a8b4",
                    ),
                ),
            }
        ),
        "purposes": ("#72b delta-baseline gate", "PLAYBACK-ATTR-3-CUDA"),
        "authority": types.MappingProxyType(
            {
                "hash_convention": "sha256 of the raw line bytes excluding the terminator",
                "consent": types.MappingProxyType(
                    {
                        "transcript": r"C:\Users\obabalola\.claude\projects"
                        r"\C---Layi-Wkspc-MLV-App--claude-worktrees-peaceful-rubin-701ecd"
                        r"\a482bbe6-87e2-4332-970e-8accb08b1663.jsonl",
                        "line": 1739,
                        "sha256": "461af7a5291099e009230a8abcfe547e064435287a23f40cf93e5ba4ddb0a066",
                        "timestamp": "2026-09-16T16:42:09Z",
                        "answers_line": 1718,
                        "answers_line_sha256": (
                            "88484275b4ef158bdaf0a98a3755c70fb65be34c514ae406c6603446dee4a38a"
                        ),
                    }
                ),
                "directive_no_manual_paths": types.MappingProxyType(
                    {
                        "transcript": r"C:\Users\obabalola\.claude\projects"
                        r"\C---Layi-Wkspc-MLV-App--claude-worktrees-happy-ptolemy-9fe12d"
                        r"\f9010ab1-55f2-4f4c-967c-467cf19c04ef.jsonl",
                        "line": 2137,
                        "sha256": "254075c85a821758067f7d37dbc5a26a05ae8e2a2368f1e02af0fa2110ac8e41",
                    }
                ),
                "scope_derivation": (
                    "ids derived by the hub from tools/gates/output-budget.json at abcf1697; "
                    "M02-1344 excluded (not acquired); purposes #72b delta-baseline gate and "
                    "PLAYBACK-ATTR-3-CUDA from the consent exchange"
                ),
            }
        ),
    }
)


def authorized_clip(ctx):
    """The ONE canonical absolute path from the lane prompt, or None."""
    if not ctx.lane_prompt:
        return None
    try:
        with open(ctx.lane_prompt, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except Exception:
        return None
    match = _CLIP_LINE_RX.search(body)
    if not match:
        return None
    value = match.group(1).strip().strip("`").strip('"').strip("'")
    if not value or value.lower() == "none":
        return None
    return norm(value)


def rule_na4(ctx):
    allowed = authorized_clip(ctx)
    fixture_root = norm(os.path.join(ctx.worktree_root_raw, FIXTURE_TAIL))
    for token in tokens(ctx.subject):
        path_norm = norm(token)
        is_clip = path_norm.endswith(".mlv")
        in_cache = under(path_norm, ctx.clip_cache_root) or has_seg(
            path_norm, ctx.clip_cache_root.lstrip("/")
        )
        if not is_clip and not in_cache:
            continue
        if under(path_norm, fixture_root) or has_seg(path_norm, FIXTURE_TAIL):
            continue  # tracked fixtures are always allowed
        if allowed is not None and path_norm == allowed:
            continue
        # No exception (3) (round 6, NARROW): a consented-footage path in command text is
        # denied here exactly as on master.  THE ROUTE THIS CARD PROVIDES for consented footage
        # is tracked, id-addressed consumers that verify content against OWNER_CONSENTED_FOOTAGE.
        # THAT IS NOT THE ONLY WAY A CONSENTED CLIP CAN BE OPENED TODAY: the NA-4 exception (2)
        # ``continue`` DIRECTLY ABOVE admits a consented clip's own path on the CLIP_OR_NONE
        # line, with NO content check against the table and by no id-addressed consumer.  See
        # the LIMITS block above OWNER_CONSENTED_FOOTAGE.
        if allowed is None:
            raise Deny(
                "NA-4",
                "no CLIP_OR_NONE authorization for %s (fixtures only)" % path_norm,
            )
        raise Deny(
            "NA-4",
            "clip %s is not the authorized path %s (a same-basename clip is a different clip)"
            % (path_norm, allowed),
        )


# ------------------------------------------------------------------------- NA-6

_ASSERT_RX = re.compile(r"\bQVERIFY\b|\bQCOMPARE\b|\bassert\b")
_QSKIP_RX = re.compile(r"\bQSKIP\b")
_RUN_HEAD_RX = re.compile(r"^([ \t]*)run:[ \t]*(.*)$")
_BLOCK_SCALAR_MARKERS = ("|", "|-", "|+", ">", ">-", ">+", "")
_TEST_CPP_RX = re.compile(r"\btest_[\w./\\-]*\.cpp\b", re.I)
WORKFLOW_TAIL = ".github/workflows"


def _indent_of(line):
    return len(re.match(r"^[ \t]*", line).group(0).expandtabs(4))


def _run_bodies(text):
    """Every ``run:`` body in a workflow fragment, whitespace-normalised.

    A block scalar ends at the first non-blank line indented no more than its ``run:``
    key -- getting that boundary wrong would swallow the following job and make a MOVE
    look like a removal (or the reverse), which is precisely the distinction NA-6's
    exception turns on.
    """
    bodies = []
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        head = _RUN_HEAD_RX.match(lines[index])
        if not head:
            index += 1
            continue
        indent = _indent_of(head.group(1))
        rest = head.group(2).strip()
        index += 1
        if rest not in _BLOCK_SCALAR_MARKERS:
            bodies.append(re.sub(r"\s+", " ", rest).strip())
            continue
        block = []
        while index < len(lines):
            line = lines[index]
            if line.strip() and _indent_of(line) <= indent:
                break
            block.append(line.strip())
            index += 1
        body = re.sub(r"\s+", " ", " ".join(block)).strip()
        if body:
            bodies.append(body)
    return bodies


def rule_na6(ctx):
    if ctx.tool in SHELL_TOOLS:
        acts = shell_acts(ctx.command)
        if "delete" not in acts:
            return
        for token in tokens(ctx.command):
            path_norm = norm(token)
            if has_seg(path_norm, "tests") or path_norm.startswith("tests/"):
                raise Deny("NA-6", "deleting %s removes a test" % path_norm)
        return

    path_norm = ctx.path_norm
    if not path_norm:
        return
    new_text = ctx.new_text or ""
    old_text = ctx.old_text

    if has_seg(path_norm, WORKFLOW_TAIL):
        if "continue-on-error" in new_text.lower():
            raise Deny("NA-6", "continue-on-error in a workflow makes a red test green")
        if old_text is not None:
            # The predicate is NET removal across .github/workflows/*.yml, never per-edit:
            # a step MOVED with a byte-identical `run:` body removes nothing (O108).
            after = _run_bodies(new_text)
            removed = [body for body in _run_bodies(old_text) if body not in after]
            if removed:
                raise Deny("NA-6", "net removal of a workflow test step: %r" % removed[0][:80])
        return

    if path_norm.endswith(".pro") and old_text is not None:
        after = set(_TEST_CPP_RX.findall(new_text))
        removed = [name for name in _TEST_CPP_RX.findall(old_text) if name not in after]
        if removed:
            raise Deny("NA-6", "removal of %s from a .pro manifest" % removed[0])
        return

    if has_seg(path_norm, "tests") or path_norm.startswith("tests/"):
        if old_text is None:
            if ctx.tool == "Write" and ctx.path and os.path.isfile(ctx.path):
                try:
                    with open(ctx.path, "r", encoding="utf-8", errors="replace") as handle:
                        old_text = handle.read()
                except Exception:
                    raise Deny("NA-6", "the existing test at %s is unreadable" % path_norm)
            else:
                return  # a NEW test file weakens nothing
        if _QSKIP_RX.search(new_text) and not _QSKIP_RX.search(old_text):
            raise Deny("NA-6", "QSKIP added to %s" % path_norm)
        before = len(_ASSERT_RX.findall(old_text))
        after = len(_ASSERT_RX.findall(new_text))
        if after < before:
            raise Deny(
                "NA-6",
                "assertion count in %s falls from %d to %d" % (path_norm, before, after),
            )


# ------------------------------------------------------------------------- NA-7

FACTORY_TAIL = ".factory"
_SED_I_RX = re.compile(r"\bsed\b[^\n;|&]*?\s-i\b[^\n;|&]*", re.I)
_COPY_RX = re.compile(r"(?:^|[\s;&|`(])(?:cp)\b[^\n;|&]*|\bcopy-item\b[^\n;|&]*", re.I)
_SET_CONTENT_RX = re.compile(
    r"\b(?:set-content|out-file|add-content|clear-content)\b[^\n;|&]*", re.I
)
_REDIRECT_DEST_RX = re.compile(r"(?<![\-=!<])>>?\s*(['\"][^'\"]+['\"]|[^\s;|&()<>]+)")
_TEE_RX = re.compile(r"(?:^|[\s;&|`(])tee\b[^\n;|&]*", re.I)


def _write_destinations(command):
    """Every path this command text could WRITE to."""
    dests = []
    for match in _REDIRECT_DEST_RX.finditer(command):
        dests.append(match.group(1))
    for pattern in (_SED_I_RX, _COPY_RX, _SET_CONTENT_RX, _TEE_RX):
        for match in pattern.finditer(command):
            segment = match.group(0)
            candidates = [
                token
                for token in tokens(segment)[1:]
                if not token.startswith("-") and ("/" in token or "\\" in token)
            ]
            if pattern is _COPY_RX and len(candidates) > 1:
                candidates = candidates[1:]  # cp SRC DST -- only DST is written
            dests.extend(candidates)
    return dests


def _na7_check_path(ctx, path_norm, how):
    # HOOK-FALSE-POSITIVE-1: a bit bucket is a SINK, not a destination this rule protects.
    # `2>/dev/null` writes no file anywhere, and refusing it as "a write outside both roots"
    # denied the commonest read-only shape on this board (measured 2026-09-07/08).
    if path_norm in NULL_DEVICE_SINKS:
        return
    # AN UNRESOLVABLE REFERENCE IS A DENIAL, NEVER A COLLAPSE.  `norm` has already
    # substituted every name THIS HOOK READS at the value it is using; a reference still
    # standing here is one the hook cannot resolve -- a name it never reads, or one of the
    # two it reads that has no default and is unset -- so this text is NOT a directory and
    # nothing downstream may reason about it as one.  MEASURED: `norm` used to collapse
    # `"$env:MLV_LANE_PROMPT/../outside.txt"` to the bare relative `outside.txt`, which the
    # early return below waved through as "inside the worktree by construction", while the
    # shell resolves the same text to an absolute path outside every root.  `norm` now leaves
    # such a path uncollapsed and the refusal is here, where the roots are decided.
    if _unresolved_env_ref(path_norm):
        raise Deny(
            "NA-7",
            "%s to a path carrying an UNRESOLVABLE environment reference (%s): this hook "
            "cannot resolve `%s`, so the destination is not a path it can bound"
            % (how, path_norm, _unresolved_env_ref(path_norm)),
        )
    if has_seg(path_norm, FACTORY_TAIL):
        raise Deny("NA-7", "%s into .factory/ is never authorized (%s)" % (how, path_norm))
    if not is_absolute(path_norm):
        # S133: "resolves inside the worktree by construction" is true only while the
        # relative path stays inside it.  `norm` has already collapsed `..`, so a canonical
        # form that STILL begins `..` is one that walks out of whatever it is relative to --
        # the same escape the absolute rows below refuse, spelled without a root.
        if path_norm == ".." or path_norm.startswith("../"):
            raise Deny(
                "NA-7",
                "%s walks out of the worktree with `..` (%s)" % (how, path_norm),
            )
        return  # a relative destination resolves inside the worktree by construction
    if under(path_norm, ctx.worktree_root) or under(path_norm, ctx.board_root):
        return
    # THE ROOTS ARE THESE TWO AND NO OTHER.  A sibling repository -- the fleet doctrine bus
    # included -- is OUTSIDE them and falls through to the refusal below, exactly as it did
    # before HOOK-FALSE-POSITIVE-1.  Publishing to the bus is a git operation performed by
    # the orchestrator in that repository; see this file's header before adding a third root.
    raise Deny(
        "NA-7",
        "%s outside both the worktree and the board root (%s)" % (how, path_norm),
    )


def rule_na7(ctx):
    if ctx.tool in SHELL_TOOLS:
        dests = _write_destinations(ctx.command)
        acts = shell_acts(ctx.command)
        if dests or acts:
            for token in tokens(ctx.command):
                path_norm = norm(token)
                if has_seg(path_norm, FACTORY_TAIL):
                    raise Deny(
                        "NA-7", "writing into .factory/ is never authorized (%s)" % path_norm
                    )
        for dest in dests:
            _na7_check_path(ctx, norm(dest), "write")
        return
    if ctx.path_norm:
        _na7_check_path(ctx, ctx.path_norm, "%s" % ctx.tool)


# ------------------------------------------------------------------------- NA-8

_NA8_RX = re.compile(
    r"(?:\bset-itemproperty\b|\breg\b\s+add\b)[^\n;|&]*usergpupreferences"
    r"|\busergpupreferences\b[^\n;|&]*(?:\bset-itemproperty\b|\breg\b\s+add\b)"
    r"|\bpowercfg\b",
    re.I,
)


def rule_na8(ctx):
    if ctx.tool not in SHELL_TOOLS:
        return
    if _NA8_RX.search(ctx.command):
        raise Deny("NA-8", "the owner's graphics/power machine state is never an agent's to mutate")


# ------------------------------------------------------------------------- NA-9

_PROTECTION_RX = re.compile(
    r"\bgh\b[^\n]*\bapi\b[^\n]*(?:-X|--method)\s*(DELETE|PATCH|PUT)\b", re.I
)


def _rsc(document):
    if not isinstance(document, dict):
        return None
    inner = document.get("required_status_checks")
    if isinstance(inner, dict):
        return inner
    if "checks" in document or "contexts" in document:
        return document
    return None


def check_set(document):
    """-> {'contexts': {name: app_id_or_None}, 'strict': bool_or_None} or None."""
    section = _rsc(document)
    if section is None:
        return None
    contexts = {}
    checks = section.get("checks")
    legacy = section.get("contexts")
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                contexts[item["context"]] = item.get("app_id")
            elif isinstance(item, str):
                contexts[item] = None
            else:
                return None
    elif isinstance(legacy, list):
        for item in legacy:
            if not isinstance(item, str):
                return None
            contexts[item] = None
    else:
        return None
    if not contexts:
        return None
    return {"contexts": contexts, "strict": section.get("strict")}


def load_snapshot(ctx):
    """LAST non-empty row of the append-only JSONL snapshot, or None (fail closed)."""
    try:
        with open(ctx.snapshot_raw, "r", encoding="utf-8") as handle:
            rows = handle.read().splitlines()
    except Exception:
        return None
    payloads = []
    for row in rows:
        if not row.strip():
            continue
        try:
            payloads.append(json.loads(row))
        except Exception:
            return None  # a malformed row ANYWHERE fails closed
    if not payloads:
        return None
    return check_set(payloads[-1])


def _canonical_04b(body):
    if body is None:
        return False
    if set(body["contexts"]) != set(CANONICAL_04B_CONTEXTS):
        return False
    if body.get("strict") is not True:
        return False
    return all(app_id == CANONICAL_04B_APP_ID for app_id in body["contexts"].values())


def _04b_receipts_ok(ctx):
    falsifier = read_json(ctx.receipt("0.4a-batch-compile-falsifier.json"))
    if not isinstance(falsifier, dict) or falsifier.get("failingContext") != "Batch Compile":
        return False
    guardrail = read_json(ctx.receipt("0.4c-guardrail-move.json"))
    if not isinstance(guardrail, dict) or guardrail.get("conclusion") != "success":
        return False
    return not os.path.exists(ctx.receipt("0.4b-required-checks.json"))


def rule_na9(ctx):
    if ctx.tool not in SHELL_TOOLS:
        return
    match = _PROTECTION_RX.search(ctx.command)
    if not match:
        return
    lowered = ctx.command.lower()
    if "branches/" not in lowered or "protection" not in lowered:
        return
    method = match.group(1).upper()

    snapshot = load_snapshot(ctx)
    if snapshot is None:
        raise Deny(
            "NA-9",
            "the required-checks snapshot is absent or unparseable; every protection "
            "mutation is denied while it is",
        )
    if method == "DELETE":
        raise Deny("NA-9", "deleting branch protection removes every required context")

    body = None
    for blob in json_blobs(ctx.command):
        try:
            candidate = check_set(json.loads(blob))
        except Exception:
            continue
        if candidate is not None:
            body = candidate
            break
    if body is None:
        raise Deny("NA-9", "no readable required-check body; add-only cannot be proven")

    removed = sorted(set(snapshot["contexts"]) - set(body["contexts"]))
    weakened = snapshot.get("strict") is True and body.get("strict") is not True
    for name, app_id in snapshot["contexts"].items():
        if app_id is not None and name in body["contexts"] and body["contexts"][name] != app_id:
            weakened = True
    if not removed and not weakened:
        return  # add-only, judged against the last recorded set
    if _canonical_04b(body) and _04b_receipts_ok(ctx):
        return  # the single recorded 0.4b transition
    if removed:
        raise Deny("NA-9", "removes required context(s) %s" % ", ".join(removed))
    raise Deny("NA-9", "weakens the required-check binding (strict or app_id)")


# ------------------------------------------------------------------------ NA-10
#
# O129 (hub extension) -- A LANE EDITING ITS OWN GATE.  This hook is re-read from disk on
# every tool call, and so is `.claude/settings.json`: an editing lane that rewrites either
# one is not making a change that takes effect at some later merge, it is making a change
# that takes effect on ITS NEXT TOOL CALL.  Nothing else in the register covered it -- NA-2
# guards `.claude-state/**` and `.claude/ANALYSIS_LOG.md`, NA-7 explicitly ALLOWS the lane's
# own worktree, and NA-6 is about tests -- so the three files that constitute the gate were
# the one part of the tree the gate did not defend.
#
# The predicate is (TAIL, VENUE) and nothing else.  UNDER ANY ROOT, because every lane
# worktree carries its own copy and it is that copy which governs that lane; and denied
# unless `--project-dir` equals the board root, because hook evolution stays hub-authored at
# the board venue, where the hub's own subagents run.  The board-venue ALLOW is not a
# loophole a lane can reach: the wrapper refuses `workdir-is-board-root`, so no lane is ever
# started there, and the argument is fixed by the registration a lane may no longer edit.
#
# FAIL-CLOSED READINGS (recorded, 0.05 fifth review delta):
#   * The shell arm scans BOTH the parsed write destinations AND -- whenever the command
#     exhibits any destructive act at all -- every token of the command.  A delete or a move
#     of the gate is as much "a lane editing its own gate" as a write is, and the register's
#     word is "any write" only because deleting your own hook is the same act with fewer
#     steps.  Over-scanning costs a visible DENY; under-scanning costs the gate.
#   * The LIMIT is unchanged and restated: an interpreter one-liner is invisible to a
#     text-matching hook.  The layers behind this rule are the wrapper's `hook-drifted` and
#     `hook-unregistered` refusals at dispatch and the sol review of every PR diff.
#
# NA-10 runs LAST, in register order.  Every specified row is attributable there: NA-7
# allows a lane's own worktree by construction, and none of the four tails is an NA-2
# protected path, so nothing else fires first and steals the reason line.


def _na10_guarded(path_norm):
    """Does this path END with one of the four gate tails, under any root?"""
    if not path_norm:
        return False
    return any(
        path_norm == tail or path_norm.endswith("/" + tail) for tail in NA10_GUARDED_TAILS
    )


def _na10_deny(ctx, path_norm, how):
    raise Deny(
        "NA-10",
        "%s %s edits this lane's OWN gate, which is re-read on every tool call; it is "
        "allowed only for the board-rooted actor and --project-dir is %s, not %s (O129)"
        % (how, path_norm, ctx.project_dir or "<absent>", ctx.board_root),
    )


def rule_na10(ctx):
    if ctx.at_board_venue:
        return  # hook evolution is hub-authored, at the board venue
    if ctx.tool in FILE_TOOLS:
        if _na10_guarded(ctx.path_norm):
            _na10_deny(ctx, ctx.path_norm, ctx.tool)
        return
    candidates = [norm(dest) for dest in _write_destinations(ctx.command)]
    if shell_acts(ctx.command):
        candidates.extend(norm(token) for token in tokens(ctx.command))
    for path_norm in candidates:
        if _na10_guarded(path_norm):
            _na10_deny(ctx, path_norm, "a shell write")


RULES = (
    rule_na1,
    rule_na2,
    rule_na3,
    rule_na4,
    rule_na6,
    rule_na7,
    rule_na8,
    rule_na9,
    rule_na10,
)


# -------------------------------------------------------------------------- main


class _ArgvError(Exception):
    """argparse wanted to exit; this hook owns its exit codes and its one stderr line."""


class _HookArgumentParser(argparse.ArgumentParser):
    """argparse that RAISES instead of exiting.

    Two reasons, both contractual.  (1) The contract is exit 2 with exactly ONE line on
    stderr; argparse's own failure path exits 2 with a usage block of several lines, which a
    reader would have to parse to tell a rejected argument from a fired rule.  (2) An
    UNKNOWN argument must be a hook-error, never a silent acceptance: ``parse_known_args``
    would swallow a mistyped ``--project-dir`` and leave the hook running with no venue,
    which is the fail-open shape O126 exists to close.
    """

    def error(self, message):
        raise _ArgvError(message)

    def exit(self, status=0, message=None):
        raise _ArgvError(message or "argument parsing stopped")


def parse_argv(argv):
    """-> the ``--project-dir`` value (possibly None).  Raises ``_ArgvError`` on anything else."""
    parser = _HookArgumentParser(
        prog="mlv-never-authorized.py", add_help=False, allow_abbrev=False
    )
    parser.add_argument("--project-dir", dest="project_dir", default=None)
    return parser.parse_args(argv).project_dir


def decide(payload, project_dir):
    """-> (exit_code, stderr_line).  Never raises for a rule; raises only on a bug."""
    if not isinstance(payload, dict):
        return EXIT_DENY, "hook-error: payload was not a JSON object"
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return EXIT_DENY, "hook-error: missing tool_name"
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return EXIT_DENY, "hook-error: tool_input was not an object"
    if tool not in MATCHED_TOOLS:
        return EXIT_ALLOW, ""
    try:
        ctx = Ctx(tool, tool_input, project_dir)
        for rule in RULES:
            rule(ctx)
    except Deny as deny:
        if deny.rule == "hook-error":
            return EXIT_DENY, "hook-error: %s" % deny.reason
        return EXIT_DENY, "%s: %s" % (deny.rule, deny.reason)
    return EXIT_ALLOW, ""


def main():
    try:
        project_dir = parse_argv(sys.argv[1:])
    except _ArgvError as error:
        sys.stderr.write(
            "hook-error: unusable command line (%s); the registered command is "
            "`<python> mlv-never-authorized.py --project-dir <path>`\n"
            % str(error).replace("\n", " ")
        )
        return EXIT_DENY
    try:
        raw = sys.stdin.read()
    except Exception as error:
        sys.stderr.write("hook-error: stdin unreadable (%s)\n" % type(error).__name__)
        return EXIT_DENY
    if not raw or not raw.strip():
        sys.stderr.write("hook-error: empty stdin\n")
        return EXIT_DENY
    try:
        payload = json.loads(raw)
    except Exception as error:
        sys.stderr.write("hook-error: stdin was not one JSON object (%s)\n" % type(error).__name__)
        return EXIT_DENY
    try:
        code, line = decide(payload, project_dir)
    except Exception as error:  # any bug in a rule denies; this hook never fails open
        sys.stderr.write("hook-error: %s: %s\n" % (type(error).__name__, error))
        return EXIT_DENY
    if line:
        sys.stderr.write(line + "\n")
    if os.environ.get("MLV_HOOK_DRYRUN") == "1":
        sys.stdout.write("DRYRUN %s %s\n" % ("ALLOW" if code == EXIT_ALLOW else "DENY", line))
    return code


if __name__ == "__main__":
    sys.exit(main())
