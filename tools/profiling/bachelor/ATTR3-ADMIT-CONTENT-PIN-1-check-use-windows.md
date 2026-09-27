# ATTR3-ADMIT-CONTENT-PIN-1 -- check/use window table (round 2g, corrected round 2h, scoped round 2i)

Round 2g responded to round 2f's two independent keys (`sol`, `fable`), both CHANGES_REQUESTED,
both converging on the same structural finding: the table itself carried the defect it exists to
prevent -- a row marked CLOSED that only narrowed, and a window missing entirely. That round
committed the table to the tracked tree for the first time, rather than living only in
round-scoped review evidence, specifically so it survives independently of any single round's
turn budget.

Round 2h responds to round 2g's own two keys (`sol`, `fable`), again both CHANGES_REQUESTED,
again converging independently: the tally round 2g computed re-introduced the exact defect the
table exists to prevent, this time in the ARITHMETIC rather than in a single row's prose (Row G
counted under CLOSED while its own status line read OPEN); a whole window (the agent's own
enumerate-then-execute step) was never enumerated; and two of round 2g's own fixes shipped
without a falsifying test. Round 2h's fixes are enumerated in each affected row/section below
and are not restated here.

Round 2i responds to `KERNEL-SAME-CLASS-STOP-RULE-1` firing -- three consecutive rounds (2f, 2g,
2h) found the same root mechanism, pathname resolution standing in for handle identity at a
rename or an execution boundary -- and the hub's required design swarm. **The ruling is NARROW:
this card lands on its content-pin substance (SUBJECT 4's four acceptance criteria, all about the
content pin itself, none about the placement race), and the remaining check/use windows are
explicitly NOT fixed here.** This round makes NO code change and adds NO mechanism; it is
documentation and scoping only. It (1) adds the generator-to-submitter window as its own row,
Row I, found independently by both keys and confirmed by the hub; (2) brings `um-run.ps1` inside
the table's own declared walk boundary, which had never named it despite the file sitting
squarely inside that boundary; (3) adds the two acceptance-branch line citations sol's round-2h
finding identified for Row F, whose prose already described both branches without citing them;
(4) corrects Row H's residual wording, which read as though a hash-sidecar provenance channel
would CLOSE that window rather than merely narrow it one step further, to the moment the child
process itself opens the pathname; (5) records sol's round-2h second blocker -- that the
HEAD-lookup and show-toplevel classification-table rows are called CLOSED despite an
unanchored-substring escape remaining live -- as REFUTED, on the strength of this table's own
pre-existing convention that the classification mechanism and the tokens it governs are tallied
separately, and makes that convention harder to miss on a skim; (6) records the deliberate
scoping decision this ruling requires, naming the dropped coverage gap by its tracking card,
`ATTR3-SHARED-SLOT-RACE-1`; and (7) records the reachability finding that decided the ruling,
together with the one measurement limit (the share's actual ACL) that finding does not close.

**Rule this table follows (round-2g ruling, extended round 2h, binding going forward): a row is
CLOSED only when the window is CLOSED, not narrowed. A mechanism is named as what it is (a
handle, an allowlist, a literal-spelling match) plus its enumerated escape hatches. Enlarging a
list and re-claiming closure is not accepted as a fix. The rule binds the TALLY as hard as the
row text: a row or token is counted exactly ONCE, by its own stated Status, never re-derived or
re-described in the tally section itself; and a residual named in a row's prose but not reflected
in that row's own Status line (an untested-but-inspected-correct code path, i.e. a coverage gap)
is never promoted into a second, separate OPEN line item in the tally -- it stays exactly where
it is named, visible to a reader of that row, and is tracked in the dedicated "Known coverage
gaps" list below the tally, which does not feed the CLOSED/OPEN count.**

**Line-number citation convention (round 2h, item 7):** this table previously cited PRE-round-2g
line numbers for lines round 2g itself had just moved (`AttrCudaArtifacts.psm1:465` for a call
round 2g moved to line 482; `:518-527` for a call it moved to `:541-549`) -- a table meant to
outlive rounds cannot cite a ref it does not name. Citations into `AttrCudaArtifacts.psm1` below
are by function name and the anchoring code's own literal text, which does not drift when
unrelated lines are inserted elsewhere in the file. Citations into `attr3-stage-fixture-job.ps1`
(Rows E and F) are still given as line numbers, because that file is untouched this round and
those numbers are accurate as of this round's HEAD -- but they carry the same staleness risk the
day that file next changes, and are equally in scope for the symbol-name treatment then.

**Walk boundary (round 2h, item 3 -- stated explicitly for the first time; the table's four prior
rounds never declared this and so could not be checked for completeness by anyone, including
their own authors):** this table walks the PRODUCER side only, from admission through to the
final write of an artifact this round considers "published" -- `Get-UmRunFixtureAdmission` ->
`Test-UmRunFixtureContentPin` -> `Assert-AttrCudaFixtureCommittedBytes` (admission) ->
`um-run.ps1`'s submission call (round 2i, new -- this file sits squarely inside the boundary
already declared here and had never been named in this table before this round; see Row I) --
in this seam `um-run.ps1` carries no hash of the job script and holds no handle on it, only the
bare pathname string crosses from its caller into `Invoke-UmRunDrop` -- ->
`Invoke-UmRunDrop`'s pre-copy re-hash, share-side `.sidepart` hash-then-rename, and job-file
hash-then-rename (placement onto the agent's SMB share) -> `attr3-stage-fixture-job.ps1`'s
generator-time bake AND the emitted job's own arrival-hash and cache-`.partial` hash-then-rename
(generator + emitted-job execution) -> `ultra-magnus-agent.ps1`'s inbox enumeration and execution
of that placed job file (round 2h, new -- see Row H). **It stops at the moment a job's own
`Write-Output`/exit-code result is produced; it does NOT walk the CONSUMING side** --
`playback-attr-3-cuda-job.ps1`'s own re-verification of the cache it reads from (mentioned only
as a downstream mitigation in Row F, never as its own row), the outbox `.result.json`
publication back to the VM, or anything the VM itself does with a result once received. That
consuming-side path is a materially different trust boundary (result data flowing back off the
measurement host, rather than fixture/job bytes flowing onto it) and was judged out of this
card's scope; it is named here as unexamined, not as closed, and not as absent from the codebase.

## Row A -- admission's internal read: git-blob drift compare + SHA256 pin derivation

- **Mechanism:** `Assert-AttrCudaFixtureCommittedBytes` opens the fixture ONCE via
  `[IO.File]::Open($full, FileMode.Open, FileAccess.Read, FileShare.Read)` -- no Write, no Delete
  share -- reads it into one buffer, and derives both the git-blob drift compare and the returned
  SHA256 pin from that same buffer.
- **Escape hatches:** none identified. `FileShare.Read` alone (no Delete) denies both a concurrent
  write AND a concurrent rename/delete of this exact path for the handle's whole lifetime.
- **Status: CLOSED.** Round 2d closed this structurally (one buffer, one handle). Round 2e added
  `test_the_source_derives_both_hashes_from_one_read_buffer` and
  `test_a_deny_write_handle_blocks_a_concurrent_writer_for_its_whole_lifetime`, proving the
  mechanism rather than merely asserting it.
- **Residual (coverage gap, not a defect):** `Get-AttrCudaGitBlobHashFromBytes`'s
  `Process.Start`/stream-I/O catch (guards a git-binary-vanishes-mid-launch failure) has no
  dedicated test for that specific catch path.

## Row B -- admission's returned pin -> local re-hash immediately before the share copy

- **Mechanism:** `Invoke-UmRunDrop` re-hashes `$SourcePath` (SHA256, independent read) immediately
  before calling `$Copier`, and binds the result back to admission's pinned SHA256; mismatch
  throws `UMRUN_FIXTURE_CONTENT_PIN_RACE`.
- **Escape hatches:** none within this window itself -- a swap strictly BETWEEN admission
  returning and this re-hash is exactly what the bind-back catches (fail-closed, not fail-silent).
- **Status: CLOSED**, closed in round 2 (`fe6be215`), unchanged in logic since.

## Row C -- share-side `.sidepart`: round-trip hash verification -> rename

- **Mechanism:** `$Copier` copies the source to `$part`. A handle is then opened
  `[IO.File]::Open($part, FileMode.Open, FileAccess.Read, FileShare.Read -bor FileShare.Delete)`
  -- denies Write share; PERMITS Delete share (so the rename below can still succeed while the
  handle stays open). SHA256 is computed over that handle and compared to `$localSha`; on match,
  `Move-Item -LiteralPath $part -Destination $destination` is called while the SAME handle is
  still open (disposed only in the outer `finally`).
- **Escape hatch (round-2g finding, sol/fable, both keys independently): `FileShare.Delete` grants
  exactly what its name says.** Another process can delete `$part`'s directory entry (freeing the
  name) and a third process can create a brand-new file at the identical pathname, all while this
  handle is open -- the handle's Read/Write denial says nothing about the Delete/rename right it
  explicitly grants. `Move-Item` below resolves `$part` by PATHNAME at rename time, not by this
  handle's identity, so it would publish whatever now occupies that path. The new
  `test_a_write_attempt_between_the_share_hash_and_the_rename_is_denied` test proves only the
  narrower "an in-place WriteAllBytes is denied" property (confirmed by inspection: the hook does
  `[IO.File]::WriteAllBytes($p, ...)`, not a delete-then-recreate) -- it does not and cannot
  discriminate this pathname-identity race, because the deny-write handle does not defend against
  it.
- **Status: OPEN, corrected from round 2f's table (which recorded this CLOSED).** What remains:
  publishing by handle identity (e.g. a rename issued against the open handle itself, which
  requires native interop -- .NET has no managed API for a rename-by-handle) rather than by
  reopening the pathname at `Move-Item` time. Not fixed in round 2g: the correct fix is a native
  `FILE_RENAME_INFO`-via-handle call, which is a materially larger, harder-to-verify change than
  this round's scope; recording the honest OPEN state was judged the safer choice per this round's
  own ruling than shipping an unverified native-interop rename under time pressure.
- **Round 2g change:** applied the IDENTICAL mechanism (with the identical residual, documented
  above) to the job file's own temporary copy, which previously had NO verification at all --
  see the new Row G below.

## Row D -- generator bake: admission pin reused, no second independent read

- **Mechanism:** `attr3-stage-fixture-job.ps1` reuses `Get-UmRunFixtureAdmission`'s pinned SHA256
  instead of an independent second read; `-PostAdmissionHook` fires in the admission-return gap;
  mismatch throws `ATTR3_FIXTURE_CONTENT_PIN_RACE`.
- **Status: CLOSED**, closed in round 2 (`fe6be215`), unchanged in logic since. Round 2e only
  relocated the two tests exercising this race into a disposable throwaway repository (test
  hygiene, not a coverage or logic change).

## Row E -- emitted job: inbox arrival hash vs. baked pin

- **Mechanism:** the job the generator emits (runs on the measurement host, no repo checkout) reads
  the side-file that arrived in the inbox once (`$arrivedSha = Get-ShaLower $side`,
  `attr3-stage-fixture-job.ps1:211`) and compares it to `$FixtureSha256`, the value the generator
  baked into the template at generation time. Mismatch calls `Complete-Failed` (exit 4), fail-closed.
- **Escape hatches:** none within this window -- this is a single read compared to a value fixed
  before the job was ever written to disk; nothing after this read but before publication reuses
  the arrival read itself (see Row F for what happens to the bytes next).
- **Status: CLOSED.** Newly given its own row in round 2g; previously undocumented in the table
  (round-2f's table stopped at the generator bake, Row D).

## Row F -- emitted job: cache `.partial` hash-then-rename (round-2g finding, both keys)

- **Mechanism:** `Publish-AttrCudaFileCopy -Source $side -Destination $partialPath`
  (`attr3-stage-fixture-job.ps1:246`) copies the arrived side-file into the cache as `<name>.partial`
  using a plain `Copy-Item`, opening and closing its own handle. The very next statement,
  `(Get-ShaLower $partialPath) -ne $FixtureSha256` (`:247`), opens ANOTHER independent handle to
  hash it. On match, `Publish-AttrCudaFileMoveNonOverwriting -Source $partialPath -Destination
  $cachePath` (`:255`) calls `[IO.File]::Move($partialPath, $cachePath, $false)`, a THIRD,
  completely independent handle. Three separate handles on the same mutable path -- this is
  precisely the pre-round-2e shape Row C used to have (hash, close, rename, on an unheld path),
  not the "handle spans hash-to-rename" shape Row C (and now Row G) apply, so it does not even
  reach Row C's residual level of protection; nothing here denies a concurrent writer at any point.
- **This is the window sol's and fable's round-2f reports both named as missing from the table**
  (the round-2f prompt explicitly asked for "the job arrival hash" leg, which the table's declared
  path never reached).
- **Two acceptance branches omitted from round 2g's telling (sol, round 2h finding), both of
  which hash this same mutable `$cachePath` and then report SUCCESS without ever holding its
  identity across the report:**
  - **Existing-cache branch** (`if (Test-Path -LiteralPath $cachePath -PathType Leaf) { if
    ((Get-ShaLower $cachePath) -eq $FixtureSha256) { Complete-AlreadyStaged ... } }`,
    `attr3-stage-fixture-job.ps1:238-240`, near the top of the staging job, before the
    copy-to-`.partial` path above is ever reached): if the cache
    already holds a file matching `$FixtureSha256`, the job hashes it once, then calls
    `Complete-AlreadyStaged`, which publishes `result.json` naming `$cachePath` and
    `$FixtureSha256` together as a verified pair -- with the file handle from the hash already
    closed. Nothing holds `$cachePath` between that hash and the moment the published result is
    read by anything downstream; a swap in that gap is not this row's Row-C/G shape (no rename is
    even involved here), but it is the identical "hash a mutable path, then use the path's name
    as if the hash still describes it" defect this row exists to name.
  - **Lost-publish-race branch** (the `catch` after `Publish-AttrCudaFileMoveNonOverwriting`
    fails because a concurrent publisher already occupies `$cachePath`,
    `attr3-stage-fixture-job.ps1:264-265`): the job re-hashes
    `$cachePath`, and on a match calls the SAME `Complete-AlreadyStaged` as the branch above, on
    the theory that the concurrent publisher finished the identical fixture. Same shape, same
    absence of a held handle between the hash and the report.
  - Neither branch is fixed in round 2h -- they are recorded here for the first time, both OPEN,
    for the same reason as the mechanism above: closing either needs the same not-yet-existing
    "hash-then-hold" primitive, and it was already judged out of this round's scope for the
    mechanism they share.
- **Mitigation, not closure (both keys agree):** the downstream consumer,
  `playback-attr-3-cuda-job.ps1`, re-authenticates the cached clip against its own `-FixtureSha256`
  parameter and fails closed at `FIXTURE_CONTENT_MISMATCH` before attribution runs -- so a swap
  landing in this window is caught before it can influence an attribution result. That bounds
  practical impact; it does not close this window, which is this table's job to enumerate.
- **Status: OPEN.** Not fixed in round 2g: closing it to Row C/G's level (a held handle spanning
  hash-to-rename) requires a new shared helper in `AttrCudaArtifacts.psm1` (there is no existing
  "hash-then-move-under-one-handle" helper; `Publish-AttrCudaFileMoveNonOverwriting` is a generic,
  multiply-used helper whose contract would need to change for every caller, or a new sibling
  function added and re-embedded into the job template) -- judged out of this round's scope under
  its own scope-discipline directive. Recorded OPEN rather than silently narrowed.

## Row G -- job-file placement: `.job.tmp` hash-then-rename (round-2g fix, item 5)

- **Mechanism, before this round:** `Invoke-UmRunDrop` copied `$ScriptPath` to `$tmp` and called
  `Move-Item -LiteralPath $tmp -Destination $final` with **no verification of any kind** --
  contradicting the module's own header invariant (which claimed every share-side temporary is
  re-read and verified before its rename). This was the single executable artifact placed into the
  inbox, placed unverified.
- **Mechanism, after this round:** the identical mechanism Row C uses -- one handle, opened
  `FileShare.Read -bor FileShare.Delete` (deny-write, permit-delete), spans the round-trip SHA256
  hash and the `Move-Item` rename. Mismatch throws the new token `UMRUN_JOB_VERIFY_FAILED`.
- **Escape hatch:** identical to Row C's -- `FileShare.Delete` permits a delete-and-recreate at
  `$tmp`'s pathname while the handle is held, and `Move-Item` resolves that pathname, not the
  handle's identity.
- **Status: OPEN, but materially improved from round 2f's "no verification at all."** Closing the
  residual requires the same native-interop rename-by-handle work named as out of scope for Row C;
  applying it here inherits, rather than introduces, that limitation.
- **Falsifier added round 2h (item 5):** `test_job_bytes_altered_on_the_share_are_refused_and_the_job_is_not_placed`
  (`test_um_run_sidefiles.py`) drives a `$Copier` that corrupts `$tmp` after the copy; asserts
  `UMRUN_JOB_VERIFY_FAILED` is thrown and the corrupted job never reaches `inbox\demo.job.ps1`.
  Manually verified to go RED (silent `NO_THROW`, corrupted job placed unnoticed) against a
  scratch copy of the module with this round's mechanism reverted to the pre-round-2g shape.

## Row H -- job execution: inbox enumeration -> `Start-Process` (round-2h finding, item 2)

- **Mechanism:** `ultra-magnus-agent.ps1`'s poll loop lists `*.job.ps1` in the inbox
  (`Get-ChildItem $inbox -Filter *.job.ps1 -File`), then for each result calls `Start-Process
  -FilePath $psExe -ArgumentList @(..., "-File", $jobPathArgument) ...` against that same
  `FileInfo`'s `.FullName`. Between the listing and the moment the child `pwsh`/`powershell`
  process actually opens and reads that path to execute it, nothing in this file holds any
  handle on it at all -- not even the deny-write handle Row C/F/G use elsewhere in this table.
  Row G's hash-then-rename verification runs entirely at PLACEMENT time, on the far side of the
  SMB share; nothing re-authenticates the file's content at EXECUTION time, an arbitrarily later
  moment (bounded only by `$PollSeconds` and however long the job sits queued behind earlier
  jobs in the same poll pass).
- **This is the window the hub named as missing entirely: the tracked table never mentioned
  `ultra-magnus-agent.ps1` once.** Every other row in this table ends at a file landing on the
  share, verified at the moment it landed; this is the only row about what happens to that file
  between landing and running.
- **Escape hatches:** the whole window, end to end -- the job pathname is mutable for its entire
  residence in the inbox, and `Start-Process`/`-File` resolves it by pathname at whatever moment
  the child process gets around to opening it, exactly like `Move-Item` resolves `$part`/`$tmp` by
  pathname in Row C/G. A write to that pathname any time after Row G's verification and before
  the child's own read -- by any process capable of writing to the share, not necessarily an
  adversarial one; a second submitter reusing a name, a retry, a stray script -- runs unverified.
  No upstream check bounds this: Row G proves the bytes were correct AT PLACEMENT, which is
  precisely what this window is positioned after.
- **Status: OPEN. Not fixed in round 2h** -- closing it needs a new provenance channel this
  protocol does not currently have (the agent has no expected hash to check the job against at
  execution time; Row G's verification is consumed entirely at placement and produces no artifact
  the agent could re-check later, unlike, e.g., a hash sidecar written alongside the job and
  re-read immediately before `Start-Process`). **Wording corrected, round 2i, item 4: that sidecar
  channel is named here only as a POSSIBLE remedy, not as one that would CLOSE this window on its
  own.** Re-hashing a sidecar immediately before `Start-Process` still leaves a race until the
  moment the CHILD process itself actually opens and reads the pathname -- the same
  pathname-resolution-rather-than-handle-identity shape this whole table exists to name, simply
  moved one step later, from "the agent's enumeration is stale" to "the agent's own re-check is
  stale by the time the child process gets around to opening the file." A sidecar narrows the
  window; it does not close it, absent the same native rename/open-by-handle work named as out of
  scope for Rows C and G. Adding even the narrowing channel is a two-sided protocol change
  (`Invoke-UmRunDrop` plus `ultra-magnus-agent.ps1`) of comparable size to round 2g's Row G fix,
  and was judged out of this round's scope under the same scope-discipline directive Row F and
  Row C's own residuals were judged under. Recorded OPEN, not CLOSED on the strength of Row G's
  placement-time check, which is exactly the upstream-check reasoning this item warned against.

## Row I -- generator-to-submitter: emitted job file handoff into `Invoke-UmRunDrop` (round 2i, item 1)

**Position note:** chronologically this window opens BEFORE Row G's mechanism runs (the job file
must reach `Invoke-UmRunDrop` before `Invoke-UmRunDrop` can hash and place it) and before
`um-run.ps1`'s seam named in the walk boundary above. It is lettered I, after Row H, rather than
inserted and renumbered between F and G, so that no existing row's letter or cross-reference in
this table (including the tally and the known-coverage-gaps list) drifts.

- **Mechanism:** `attr3-stage-fixture-job.ps1` -- here acting as the GENERATOR process, not as the
  emitted job's own runtime body that Rows D/E/F describe -- writes the finished job script to
  disk with `[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))`
  (`attr3-stage-fixture-job.ps1:316`), which opens, writes, and disposes its own handle internally
  and holds nothing across the call. The generator process's own involvement ends there (or
  continues on to other work); nothing it does links `$jobPath`'s bytes to any later read of that
  pathname. Some later, unbounded, cross-process gap follows -- `um-run.ps1:59` calls
  `Invoke-UmRunDrop -Inbox $inbox -Outbox $outbox -ScriptPath $ScriptPath -JobId $JobId -SideFile
  $SideFile`, passing `$ScriptPath` (the same path as `$jobPath`) as a bare pathname string; no
  hash and no handle travel with it, per the walk-boundary note on `um-run.ps1`'s seam above.
  Inside `Invoke-UmRunDrop`, `$jobLocalSha = (Get-FileHash -LiteralPath $ScriptPath -Algorithm
  SHA256).Hash` (`UmRunDrop.psm1:491`) opens a brand-new, independent handle to that same pathname
  and hashes whatever now occupies it -- this is the value Row G's mechanism later treats as
  ground truth for the share round-trip check.
- **Escape hatches:** the entire window, end to end. Nothing binds the generator's `WriteAllText`
  output bytes to `Get-FileHash`'s later read at `UmRunDrop.psm1:491` -- not a returned hash
  compared on the far side (the shape Row B uses for the fixture), not a held handle spanning both
  events (the shape Rows C/G use for their own narrower residual), not even a size or mtime check.
  Whoever can write to `$jobPath` between the two events -- a retry, a stray script, a second
  submitter reusing the path, the generator's own caller substituting a different file at the same
  name -- determines what `$jobLocalSha` actually pins, and every downstream check in this table
  (Row G's share round-trip, Row H's eventual execution) verifies AGAINST that possibly-substituted
  value, not against what the generator actually wrote.
- **Status: OPEN.** Newly named this round (round 2i, item 1; found independently by both keys and
  confirmed by the hub); not fixed -- this round makes no code change of any kind. Closing it would
  need the generator to return a hash that `um-run.ps1` threads through to `Invoke-UmRunDrop` for a
  bind-back check (Row B's shape, adapted across a process boundary rather than within one), since
  holding one handle across two separate script invocations is not possible as currently
  structured. Judged out of this round's scope under the same scope-discipline directive Rows C,
  F, G, and H's own residuals were judged under.
- **Reachability (round 2i, item 7 -- see also the dedicated note below the classification
  table):** writing to `$jobPath` between the generator's `WriteAllText` and `Invoke-UmRunDrop`'s
  read requires filesystem write access to wherever the generator stages its output before
  submission -- a location under the caller's own control, not the agent's inbox share. This does
  not by itself say the window is unreachable by a remote actor; it says the actor capable of
  winning this particular race is already positioned to control what gets submitted as the job in
  the first place, which is the same reachability shape the other rows' escape hatches share (see
  the reachability note below the classification table for the full statement and its limit).

## Round-2e/2g/2h classification theme (cross-cutting, not a single check/use window)

Splitting "could not determine admissibility" (environmental/operational failure) from "definite
refusal" (an evidenced verdict about the fixture's bytes):

| Token | Where | Status |
|---|---|---|
| `ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE` | `git rev-parse "HEAD:$relative"` inside `Assert-AttrCudaFixtureCommittedBytes` (`AttrCudaArtifacts.psm1`) fails for a reason other than genuine non-commitment | CLOSED, tested (round 2e) |
| `UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE` | `Import-Module` of a present-but-broken bachelor module | CLOSED, tested (round 2e) |
| `UMRUN_FIXTURE_ADMISSION_PATH_RESOLUTION_UNAVAILABLE` | `Get-Item`/`ResolveLinkTarget` raw throw in `Get-UmRunFixtureAdmission` | **OPEN -- coverage gap** (catch exists, no test drives the specific TOCTOU throw path) |
| `UMRUN_SIDEFILE_ADMISSION_INDETERMINATE` / `ATTR3_FIXTURE_ADMISSION_INDETERMINATE` | side-file and generator admission-refusal callers branch on `.Indeterminate` | CLOSED, tested |
| `ATTR3_FIXTURE_GIT_UNAVAILABLE` (repository-discovery leg) | **round-2g fix, round-2h anchor tightening:** `git rev-parse --show-toplevel` inside `Assert-AttrCudaFixtureCommittedBytes` (`AttrCudaArtifacts.psm1`) used to discard stderr (`2>$null`) and fold EVERY failure -- dubious ownership, corrupted config, I/O error -- into the definite `ATTR3_FIXTURE_NOT_IN_A_REPO`. Now inspects stderr the same way the HEAD:<path> lookup already did: only git's own `fatal: not a git repository` text (round 2h: anchored to the `fatal: ` prefix git itself always emits ahead of it, narrowing the round-2g match, which was a bare, unanchored `not a git repository` substring test -- the same shape as the HEAD:<path> lookup's own known-open `invalid object name` edge below) is the definite refusal; everything else is `ATTR3_FIXTURE_GIT_UNAVAILABLE` (already a registered indeterminate token) | **CLOSED, round 2g, anchor tightened round 2h. Falsifier added round 2h:** `test_a_corrupted_repo_config_is_indeterminate_not_not_in_a_repo` (`test_playback_attr_3_cuda_behaviour.py`) corrupts `.git/config` so `--show-toplevel` fails with git's own `fatal: bad config line` text and asserts `ATTR3_FIXTURE_GIT_UNAVAILABLE`, not `ATTR3_FIXTURE_NOT_IN_A_REPO`. Manually verified to go RED (throws `ATTR3_FIXTURE_NOT_IN_A_REPO`) against a scratch copy of the module with this round's stderr inspection reverted. |
| the classification mechanism itself | `UmRunDrop.psm1`'s `$UmRunIndeterminateAdmissionTokens` | **OPEN -- named honestly, round 2g/2h: this is a TOKEN ALLOWLIST over a closed, documented set of throw sites, not an exhaustive classification.** It is only as complete as every throw site actually using one of its tokens. Round 2g found and fixed one gap where a throw site used a definite token it should not have (the show-toplevel leg above). Three narrower, prose-only escape hatches remain and are named in `Assert-AttrCudaFixtureCommittedBytes`'s docstring rather than fixed: an unanchored `invalid object name` stderr match; the show-toplevel `fatal: not a git repository` match, round-2h-narrowed but still a substring match rather than a fully anchored one; and a large-fixture OOM that surfaces as a raw untokened exception instead of `ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE`. Carrying THREE live escape hatches, this row is OPEN as a whole even though two of the three tokens it governs are individually CLOSED above -- the allowlist mechanism and the tokens it classifies are tracked separately in this tally (see below). |

**Reviewer note (round 2i, item 5 -- refuted, not fixed):** sol's round-2h second blocker argued
that the HEAD-lookup and show-toplevel rows above are marked CLOSED even though an
unanchored-substring escape hatch remains live for each (the `invalid object name` match and the
round-2h-narrowed-but-still-substring `fatal: not a git repository` match respectively). **This
table already carries the answer, in the row directly above -- "the classification mechanism
itself" -- and did so before this blocker was raised.** Read plainly: this table tracks TWO
different things at TWO different granularities on purpose, and conflating them is the reading
that produces the blocker. (1) Each TOKEN row states whether that specific classification, as
coded today, is CLOSED or OPEN -- the HEAD-lookup and show-toplevel rows are CLOSED because, for
the throw sites those two fixes govern, the definite-vs-indeterminate split each describes is
correct as shipped. (2) The escape hatches sol's blocker names are not a defect IN those two rows'
own fixes -- they are pre-existing substring-match looseness in the same docstring those fixes
narrowed, and are exactly what the mechanism row directly above exists to carry: it is marked
OPEN, by name, listing all three live escape hatches (the unanchored `invalid object name` match;
the round-2h-narrowed-but-still-substring `fatal: not a git repository` match; the untokened OOM
edge), with its own prose stating explicitly that "this row is OPEN as a whole even though two of
the three tokens it governs are individually CLOSED above." **Nothing here is re-scored by this
note -- the tally is unchanged.** This note only makes the table's own pre-existing convention
(token-row granularity and mechanism-row coverage are tracked as separate lines, on purpose, and
are not double-counted or under-counted against each other) harder to miss on a skim, since a
skim is exactly what produced this blocker.

## Known-item accounting (round-2g completion of item 4)

- **`.gitattributes` clean-filter preimage -- proper mechanism row (was tally-only before this round).**
  `Get-AttrCudaGitBlobHashFromBytes` reproduces git's blob hash by piping the in-memory buffer to
  `git hash-object --stdin --path <RelativePath>`, which selects clean filters (`.gitattributes`
  `filter=`/`clean=` drivers, `core.autocrlf`) by the PATH given to `--path`, not by re-reading the
  file itself. **Known limitation, pre-existing, not investigated further this round:** an external
  filter driver whose behaviour depends on something other than piped byte content (e.g. reading
  the actual on-disk file path itself, or environment/working-directory state a real checkout
  provides that stdin does not) could disagree with what `git hash-object <path>` would compute by
  reading the file directly. No fixture in this repository currently exercises a custom external
  clean filter, so this is undefended and untested, not merely undocumented. **Status: OPEN,
  pre-existing, out of this round's scope.**
- **`core.autocrlf` in `_make_fixture_repo` -- accounted for, not a fix to the item above.**
  `test_playback_attr_3_cuda_behaviour.py`'s `_make_fixture_repo` (and the two
  `test_um_run_sidefiles.py` setUps plus `StageFixtureJobRaceTests`) pin `core.autocrlf=true`
  locally in the throwaway fixture repo. This is TEST DETERMINISM, not a code fix: it guarantees
  the suite's CRLF-normalisation fixture actually exercises git's clean-filter path on every host,
  regardless of the host's own global `core.autocrlf`, so the round-2d self-caught defect (a naive
  raw-byte SHA1 that ignored git's filters) stays caught by CI on any machine. It does not touch,
  and is not a disposition on, the clean-filter preimage gap named above -- that gap is about an
  UNTESTED kind of filter (external driver), not about whether autocrlf itself is exercised.
  **Status: test-hygiene item, CLOSED as test hygiene; does not change the disposition of the
  clean-filter preimage row above.**

## Scoping decision (round 2i, item 6)

`KERNEL-SAME-CLASS-STOP-RULE-1` fired because rounds 2f, 2g, and 2h found the same root mechanism
three rounds running -- pathname resolution standing in for handle identity at a rename or an
execution boundary -- and the hub's required design swarm ruled this card's scope NARROW. Per
RESUME.md STEP 2.5, a narrowing is recorded as a decision that names the dropped coverage gap, not
left to be inferred:

- **What this card DOES close:** the content pin itself and its fail-closed classification --
  Rows A, B, D, E of this table (the admission read, the admission-pin-to-local-rehash bind-back,
  the generator's reuse of the admission pin, and the emitted job's arrival-hash check), plus the
  classification-table tokens marked CLOSED above. This is the full scope of SUBJECT 4's four
  acceptance criteria: working-tree bytes equal the committed blob, fail-closed on mismatch, a
  RED-then-GREEN test proving it, and a non-author-verifiable green with KF-10 bookends. None of
  the four criteria mention the placement race; landing the pin satisfies the declaration as
  written.
- **What is explicitly NOT closed, deferred:** every window in this table whose defect shape is
  "verified by pathname, used by pathname, with nothing binding the two moments together" -- Row C
  (share-side `.sidepart`), Row F (cache `.partial`, both its main mechanism and its two acceptance
  branches), Row G (job-file placement), Row H (inbox execution), and Row I (the generator-to-
  submitter handoff, new this round). All five share the identical root mechanism the stop-rule
  fired on. They are tracked as **`ATTR3-SHARED-SLOT-RACE-1`**, an existing card that has sat
  `queued, owner=""` since 2026-09-18 -- the hub is re-scoping that card to cover this set rather
  than filing a duplicate, which is a hub action, not this round's.
- **The named remedy, out of scope here:** a native `FILE_RENAME_INFO`-via-handle rename (and, for
  Row H, an equivalent open-by-handle or provenance-channel primitive at execution time) would
  close all five deferred windows by binding the verified handle's identity to the rename/open
  target instead of re-resolving the pathname. `.NET` has no managed API for this; it requires
  P/Invoke against the Win32 API, is a materially larger and harder-to-verify change than any
  single fix this table's rounds have shipped so far, and was judged out of scope for every round
  that touched these rows (2g for Row C/G, this round for Row F/H/I). It remains the named remedy
  for `ATTR3-SHARED-SLOT-RACE-1` to pick up.

## Reachability finding (round 2i, item 7)

Every open row this table defers (Rows C, F, G, H, I) requires an actor who can already write to
`\\Ultra-Magnus\...\agent\inbox` -- a private single-user SMB share whose own agent header states
it *"intentionally executes scripts dropped into inbox."* **Anyone who can win one of these races
can already drop their own job file onto that share outright, which is strictly easier than timing
a rename or an execution race.** So these windows do not raise risk above what the protocol's own
documented design already accepts: a share that executes whatever lands on it is already trusting
every writer to that share with code execution, and none of these races grants a writer any
capability beyond what a direct drop already grants them.

**Limit on this finding, stated plainly so it is not read as more than it is:** the share's actual
ACL -- who, in practice, holds write access to that inbox path -- is **UNMEASURED**. It is host
configuration, not something derivable from this repository, and this round did not check it. This
finding says the races do not raise risk ABOVE the protocol's accepted design; it does not say the
protocol's accepted design is itself safe, and it is not evidence that the ACL is in fact narrow.
It is "therefore not above the accepted design risk, pending an ACL nobody here has checked" -- not
"therefore harmless."

## Tally

**Counting convention (round 2h, item 1 -- stated explicitly for the first time; round 2i adds no
new convention, only a ninth primary row under the existing one):** each primary row (A-I as of
this round) and each classification-table token/mechanism row is counted exactly ONCE, by its own
stated Status line -- never re-described or re-derived in this section. A residual named in a
row's own prose that is NOT reflected in that row's Status line (an inspected-correct-but-untested
code path, i.e. a coverage gap) is not a second, separate OPEN entry here; it lives only in
"Known coverage gaps" below, which this count explicitly excludes. This is the direct fix for
round 2g's own tally defect: round 2g's CLOSED list named Row G despite Row G's own Status line
reading "OPEN, but materially improved" one screen above it -- the tally inflating a row's
disposition past what its own prose claimed, the same shape this table exists to catch, just
moved from a single row's text into the arithmetic that summarizes all of them.

- **CLOSED (8):** Row A, Row B, Row D, Row E (4 primary rows); `ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE`,
  `UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE`, the indeterminate-branching callers, and the
  `ATTR3_FIXTURE_GIT_UNAVAILABLE` show-toplevel fix (4 classification-table rows).
- **OPEN (8):** Row C, Row F, Row G, Row H, Row I (5 primary rows -- Row G's own Status line has
  read OPEN since the sentence was first written in round 2g, round 2g's tally miscounted it as
  CLOSED, corrected round 2h; Row I is new this round, round 2i item 1); the
  `UMRUN_FIXTURE_ADMISSION_PATH_RESOLUTION_UNAVAILABLE` token and the classification allowlist
  mechanism itself, both by their own stated Status (2 classification-table rows); the
  pre-existing clean-filter preimage gap (1, tracked under "Known-item accounting" above).
- **Not tallied (test-hygiene, neither a window nor a gap):** the `core.autocrlf` fixture-repo
  pinning, tracked under "Known-item accounting" above.
- **Total: 8 CLOSED / 8 OPEN, 16 tallied.** Up by one OPEN item from round 2h's 8 CLOSED / 7 OPEN,
  15 tallied, entirely because Row I is newly named this round (round 2i, item 1) -- no existing
  row's verdict changed. No row is double-counted: Row I's addition is the only arithmetic change
  this round makes to this section.

## Known coverage gaps (not tallied; follow-up, not a defect)

Untested-but-inspected-correct code paths named in a row's own prose without appearing in that
row's Status line. Per the counting convention above, these never change a row's CLOSED/OPEN
disposition and are not counted in the tally; they are follow-up items for a future round's
test-writing budget, listed here so they stay visible rather than buried inside a CLOSED row's
paragraph.

- **Row A:** `Get-AttrCudaGitBlobHashFromBytes`'s `Process.Start`/stream-I/O catch (guards a
  git-binary-vanishes-mid-launch failure) has no dedicated test for that specific catch path.
- Round 2h closed the two other entries this list previously carried, by adding the falsifiers
  named under Row G and under the `ATTR3_FIXTURE_GIT_UNAVAILABLE` classification-table row above
  -- removed from this list here, not silently dropped from the round's record.

## Verification, round 2g

- `tools/repo_hygiene/test_um_run_sidefiles.py`: 31 passed, 10 subtests passed. Exit code 0 taken
  directly from the pytest invocation (not through a pipe).
- `tools/repo_hygiene/test_playback_attr_3_cuda_behaviour.py`: 75 passed, 103 subtests passed. Exit
  code 0 taken directly.
- `tools/repo_hygiene/test_attr3_stage_fixture_job.py`: 12 passed. Exit code 0 taken directly.
- Total: 118 passed (31 + 75 + 12), matching the round-2f hub's reported total; HEAD unchanged
  (`bbfb2943`) and `git status --porcelain` clean immediately before this round's first edit.
- **No new test was added this round** for either code fix (item 3's show-toplevel classification
  fix, or item 5's job round-trip verification) -- both are covered only by the existing suites
  continuing to pass, not by a dedicated new race/injection test. Recorded here rather than
  implied: this is a real coverage gap on both fixes, consistent with this round's own rule against
  claiming more than what was actually done.

## Verification, round 2h (item 5)

Both round-2g fixes named above as lacking a falsifier now have one, and both were manually
proven to go RED against a scratch copy of the affected module with the fix reverted (not merely
asserted to pass against the current code -- that would prove nothing an existing-suite-passes
claim doesn't already prove).

- `test_job_bytes_altered_on_the_share_are_refused_and_the_job_is_not_placed`
  (`test_um_run_sidefiles.py`): a `$Copier` corrupts `$tmp` after the copy; asserts
  `UMRUN_JOB_VERIFY_FAILED` and that the corrupted job never reaches `inbox\demo.job.ps1`.
  Verified RED (silent `NO_THROW`, corrupted job placed unnoticed at
  `inbox\demo.job.ps1`) against a scratch copy of `UmRunDrop.psm1` with the round-2g
  hash-then-rename mechanism reverted to a plain copy-then-`Move-Item`, run against a disposable
  inbox/outbox pair outside the tracked tree.
- `test_a_corrupted_repo_config_is_indeterminate_not_not_in_a_repo`
  (`test_playback_attr_3_cuda_behaviour.py`): corrupts `.git/config` with an invalid line so `git
  rev-parse --show-toplevel` fails with git's own `fatal: bad config line` text (confirmed
  empirically on this host, exit 128); asserts `ATTR3_FIXTURE_GIT_UNAVAILABLE`, not
  `ATTR3_FIXTURE_NOT_IN_A_REPO`. Verified RED (throws `ATTR3_FIXTURE_NOT_IN_A_REPO`) against a
  scratch copy of `AttrCudaArtifacts.psm1` with the round-2g stderr inspection reverted to
  `2>$null` plus an unconditional `ATTR3_FIXTURE_NOT_IN_A_REPO` on any failure.
- Full-suite re-run after both falsifiers were added, item 6's anchor tightening, and item 4's
  table-only additions (no code change for item 4): `test_um_run_sidefiles.py` 32 passed / 10
  subtests passed (was 31, +1 for the new falsifier); `test_playback_attr_3_cuda_behaviour.py` 76
  passed / 103 subtests passed (was 75, +1); `test_attr3_stage_fixture_job.py` 12 passed
  (unchanged, no code in this file was touched round 2h). Total 120 passed (32 + 76 + 12). Every
  exit code taken directly from its own `pytest` invocation, never through a pipe.
- **Not measured this round:** a dedicated test for Row H (item 2) -- it is recorded OPEN rather
  than given a falsifier, since no fix was made to falsify against (see Row H's own residual).
  The `UMRUN_FIXTURE_ADMISSION_PATH_RESOLUTION_UNAVAILABLE` TOCTOU throw path and the
  large-fixture-OOM edge remain undriven by any test, unchanged from round 2g.

## Verification, round 2i (documentation and scoping only -- no code change)

This round is narrow by ruling: it makes no code fix and adds no mechanism, so there is no
falsifier to add and no RED-then-GREEN pair to demonstrate. The gate exists here only to prove the
suite is unchanged, bracketed by HEAD and `git status --porcelain` on both sides, with every exit
code taken directly from its own `pytest` invocation, never through a pipe.

- **Before this round's first edit:** HEAD `313c0a51` (tip of round 2h, `product/ATTR3-ADMIT-CONTENT-PIN-1`),
  `git status --porcelain` clean.
- **Pre-edit gate run:** `test_um_run_sidefiles.py` 32 passed / 10 subtests passed, exit 0;
  `test_playback_attr_3_cuda_behaviour.py` 76 passed / 103 subtests passed, exit 0;
  `test_attr3_stage_fixture_job.py` 12 passed, exit 0. Total 120 passed (32 + 76 + 12), matching
  round 2h's reported total exactly.
- **Edits made this round:** this table file only -- the walk boundary, Row F (two line
  citations), Row H (wording correction), a new Row I, the classification-table reviewer note,
  the new "Scoping decision" and "Reachability finding" sections, the tally, and this section. No
  `.ps1`/`.psm1`/`.py` file was opened for editing.
- **Post-edit gate run:** `test_um_run_sidefiles.py` 32 passed / 10 subtests passed, exit 0;
  `test_playback_attr_3_cuda_behaviour.py` 76 passed / 103 subtests passed, exit 0;
  `test_attr3_stage_fixture_job.py` 12 passed, exit 0. Total 120 passed (32 + 76 + 12) --
  **unchanged from the pre-edit run, as expected for a documentation-only round.**
- **`git diff --name-only` against the pre-edit HEAD shows exactly one path:**
  `tools/profiling/bachelor/ATTR3-ADMIT-CONTENT-PIN-1-check-use-windows.md` -- confirming no code
  behaviour changed.
