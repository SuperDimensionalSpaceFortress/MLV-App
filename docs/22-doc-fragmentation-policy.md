# 22 - Documentation Fragmentation Policy

Status: **active**, adopted 2026-07-27.

> **Provenance (2026-09-22).** Restored verbatim from `8698b925` on the peer branch
> `docs/fragmentation-policy-20260727`, which never merged and was archived at
> `refs/archive/closeout-20260914/`. Its enforcement scripts had already been ported
> to master, and `CLAUDE.md`, `AGENTS.md`, `agents/*`, `claude/*` and
> `tools/docs/split_claude_md.py` all link here, so the link was dangling. Every
> count, byte size and "as of 2026-07-27" figure below is a **stale snapshot** -
> re-derive with the commands in this section, never quote it.

Enforced by two scripts, deliberately separate:

| Script | Checks | Comparable across fleet |
|---|---|---|
| [`tools/docs/run_doc_size.py`](../tools/docs/run_doc_size.py) -> the bus `check-doc-size` **run by pin** (`factory-pins/factory-pins.json`), budgets in [`tools/docs/doc-size-budget.config.json`](../tools/docs/doc-size-budget.config.json) | byte budgets by role | yes - one implementation, the pinned bus bytes; only the config is repo-specific |
| `tools/check-doc-size.py` (**retiring** - see "Pinned runs") | the same budgets, old project copy | no - its only remaining caller is the `Stop` hook below |
| [`tools/docs/check_pinned_tokens.py`](../tools/docs/check_pinned_tokens.py) | splits do not drop closeout-asserted substrings | no - this hazard is specific to this repo |

Re-derive every number below rather than trusting the snapshot:

```bash
py -3 tools/docs/run_doc_size.py --warn-only --all
```

### Pinned runs (CHECK-DOC-SIZE-SINGLE-SOURCE-1)

`run_doc_size.py` runs `python -X utf8 -I factory-pins/pin-runner.py run check-doc-size -- --config
tools/docs/doc-size-budget.config.json --repo <repo> ...`, so the bytes that execute are the bus blob
recorded in `factory-pins/factory-pins.json` (a bus push changes nothing that runs). It adds one thing the
pinned tool lacks, `--trace FILE`, the one-line run record the `Stop` hook relies on. What to know:

- **Scan = `worktree`**, not git-tracked. `.claude-state` is git-ignored and is most of the markdown mass, so
  a tracked-only scan would report compliance while the hottest files sat outside it (the `hub` tier below
  keeps its coverage). It also never decodes `git ls-files`, so a non-ASCII file name stays checked; the
  bus tool's git-tracked mode loses such names (bus card `airmypc/factory-check-doc-size-drops-non-ascii-paths`).
  `.codex-state/`, `CLAUDE_CODE_PROMPTS.md` and the two `.pytest_cache` trees outside `.claude-state` are git-ignored
  files the old tool never governed, so they are exempted to keep the findings identical.
- **A host without the bus clone.** `factory-pins.json` `bus_clone` lists one absolute path (this box's). The
  runner reads the pinned blob from a local blob cache first (`$FACTORY_PINS_CACHE`, default
  `%LOCALAPPDATA%/factory-pins/blobs`), so a pin that has run once on a host keeps running with no clone. A
  host that has neither exits **3 UNAVAILABLE** and runs nothing: set `FACTORY_BUS_CLONE` to a clone. Hosted CI
  has neither, which is why no CI step calls this tool.
- **Cache repair (bus defect, not edited here).** `pin-runner.py` rejects corrupt cache bytes but never
  repairs a same-size corruption, so a run after bus loss can fail offline (exit 3, never wrong bytes). Cure:
  delete `<cache>/<blob id>` and run once with the bus present. Reported to the bus owner in
  `doctrine-outbox/20261018-pin-runner-cache-never-repairs-same-size-corruption.md`.
- **Guard:** `tests/coordination/test_check_doc_size_by_pin.py` (pass and breach cases through the runner,
  non-ASCII names, scratch bus, no inherited `GIT_DIR`).

```bash
py -3 tools/docs/check_pinned_tokens.py
```

---

## Relationship to COORDINATION-PRUNE-POLICY (read this first)

**This policy does not supersede anything.** `.claude-state/coordination/COORDINATION-PRUNE-POLICY.md`
is a **BINDING** retention policy for the coordination state, ratified 2026-07-15
by fable + codex + claude-review, with opus acking under amendment A1. It remains
authoritative for everything under `.claude-state/coordination/`. This document
governs the rest of the tree and defers to it where they overlap.

Four of its rules are **better** than the generic framework this policy was
adopted from, and are carried forward here rather than reinvented:

- **P1 / P3 split.** The generic rule is "pruning is move-only, never delete."
  That is right for ledgers and *wrong* for machine-generated logs. P3 (LOGS ARE
  NOT FIDELITY) correctly rotates-and-deletes regenerable heartbeat/watcher/beacon
  logs. Adopted: move-only applies to authored content, not to regenerable output.
- **P6 stale-OPEN audit before archival.** A roll filters the *closed* set, so
  the closed set must be accurate first. Without P6, stale OPEN entries pin the
  whole history in the live file. The generic rule 5 has no equivalent
  precondition. Adopted.
- **P7 STATE digest.** The live head keeps a <=15-line digest so a fresh seat
  boots from digest + tail without reading archives. This is what makes a small
  live file *sufficient* rather than merely small. Adopted.
- **D1 session-lifecycle duty.** Archival runs as an EXECUTE rung when a seat
  retires - owner-executed, at a natural boundary. This is the mechanical
  enforcement the generic framework claims nobody in the fleet has achieved; this
  repo already had it, and its execution record proves it works
  (fable.md 1.25 MB -> 100 KB, opus.md 603 KB -> 134 KB).

Its 150 KB live-ledger budget is likewise adopted as-is for the `hub` tier below,
not re-derived.

---

## 1. Index or leaf, never both

Every markdown file is an **INDEX** (pointers + procedures) or a **LEAF**
(detail). A file accumulating both is the failure mode this policy targets.

## 2. Budgets by role, in bytes

Cost is proportional to how often a file is **read**, not to where it sits. Roles
are assigned by glob in `tools/docs/doc-size-budget.config.json`.

| Role | Meaning | Soft | Hard |
|---|---|---:|---:|
| `entry` | auto-loaded every session by an agent | 8 KB | 12 KB |
| `hub` | git-ignored coordination ledgers | 150 KB | 150 KB |
| `ledger` | append-only; live head + recent tail | 30 KB | 60 KB |
| `working` | read once, on demand, by one agent | 25 KB | 40 KB |

Bytes, not lines: a few enormous paragraphs are worse than many short lines.
**Soft** = plan the split. **Hard** = split before ending the turn.

`hub` has soft == hard on purpose. COORDINATION-PRUNE-POLICY ratifies a *single*
trigger at 150 KB; inventing a second threshold would be freelancing on a
multi-lane ratified policy.

Only two files are `entry` tier: `CLAUDE.md` and `AGENTS.md`. Keeping that list
tiny is what makes the budget mean anything.

> **Do not raise a budget to make a file fit.** Widening a cap to accommodate a
> file you did not want to split is the exact failure this policy prevents, and it
> is visible in the diff.

## 3. Split by demoting in place

The parent **keeps its exact path** and becomes the index. Detail moves to
children in a sibling directory named for the parent stem
(`CLAUDE.md` -> `claude/`). Never rename the parent.

Headings are **stable IDs**; the index carries a section-to-file map.

Move prose **verbatim, with a script**. Reference implementation:
[`tools/docs/split_claude_md.py`](../tools/docs/split_claude_md.py), which refuses
to write unless every moved block is byte-identical and no section is orphaned.

### 3a. Pinned strings - why splitting is dangerous here

`tools/repo_hygiene/brokered_closeout.py` and `tools/repo_hygiene/test_brokered_closeout.py`
assert exact substrings against specific doc paths. As of 2026-07-27 that is
**126 assertions across 8 documents** (28 against `CLAUDE.md`, 27 against
`AGENTS.md`, 33 against `docs/19-closeout-dashboard-spec.md`). Re-derive with
`py -3 tools/docs/check_pinned_tokens.py`.

The assertion binds a substring to a **path**, so a *move* breaks it as badly as
a rename. A broken baseline raises `closeout_tooling_stale` and blocks finalize.

**A split must keep every pinned token resident in the parent index**, on the
pointer line for the section that now holds its prose.

This is not hypothetical. The first `CLAUDE.md` split in this repo satisfied the
structured baseline, silently dropped 12 tokens pinned only by the *test suite*,
and red-lighted `test_repo_state_dashboard_and_rollback_contract_required`. The
guard now reads both surfaces, and parses the test suite via AST rather than by
pattern-matching text. Regenerate the token block with:

```bash
py -3 tools/docs/split_claude_md.py --refresh-tokens
```

## 4. Indexes carry procedures, never values

Do not paste a SHA, a timestamp, a count or any live state into an index. Write
the command that derives it. Where a snapshot genuinely helps, stamp it with an
as-of and say it must be re-derived.

This applies to the tooling: `check_pinned_tokens.py` parses the assertion lists
out of their sources on every run rather than hardcoding 126 strings, so it cannot
drift from what it protects.

**Carve-out:** hash-bound freeze attestations are values by design
(`CLOSEOUT-CANONICAL-CONTRACT.sha256` is the whole point of that file). Such a
value must carry an as-of and the command to recompute it.

## 5. Pruning is move-only, never delete

Roll an append-only file at ~60% of its cap down to ~30% (hysteresis, so it does
not roll every turn).

**A roll is a filter over the closed set, never a selection over the live set.**
Only completed items, closed rows and superseded sections leave. Facing an
over-budget live file, strike closed lines - never edit the live record to fit.

Run the P6 stale-OPEN audit *first*, so the closed set is accurate. Refresh the
P7 STATE digest after. Superseded is not deleted: mark it, say what replaced it,
keep the reasoning. Archive as `<stem>-archive-YYYYMMDD.md` with a MANIFEST line
carrying the moved block's SHA256 and a reason.

Per P3, this rule governs **authored content**. Regenerable machine logs rotate
at cap and delete beyond a bounded history; they are not fidelity.

---

## Scope

Stated explicitly so this policy cannot read as compliance while covering a
sliver of the mass. Snapshot **as of 2026-07-27**; re-derive with the commands at
the top.

| Tier | Files | Bytes | Over hard cap |
|---|---:|---:|---:|
| `entry` | 2 | 64,511 | 1 |
| `hub` | 238 | 6,024,743 | 7 |
| `ledger` | 1 | 56,062 | 0 |
| `working` | 122 | 2,357,370 | 13 |
| **Governed total** | **363** | **8,502,686** | **21** |

Tracked markdown alone is 136 files / 2,829,453 bytes. **The git-ignored
`.claude-state/` tree is 1,735 markdown files / 59,449,541 bytes - roughly 21x the
tracked corpus.** A tracked-only checker would have reported near-compliance while
the largest and hottest files sat outside the scan entirely. `EXTRA_ROOTS` in the
CONFIG block exists for exactly this reason.

### `.claude-state/`: in scope NOW, moves deferred - on the record

`.claude-state/` is governed and **reports breaches from day one**. It is not
exempt and not undeclared, because ungoverned mass reads as compliance and is
worse than an honest gap.

**Actual moves are deferred, deliberately.** The `hub` breaches are live
coordination ledgers under COORDINATION-PRUNE-POLICY's ownership rules: P5 says
only the pen owner archives its own ledger, and
`.claude-state/coordination/gpu-lane-impl-review-sync.md` (870,424 B) is
load-bearing for the `contentReviewGate` that releases finalize. Its own Phase 1
is mid-execution - the codex.md slice is still pending a NUL-byte repair.

Measuring costs nothing and breaks nothing. Moving, here, would mean executing
another lane's owner-executed duty out of order. So: report now, move when the
owning lane runs its D1 rung and an independent-lane byte-verify has run.

### Exempt, with reasons

Listed under `exempt` in `tools/docs/doc-size-budget.config.json`, each with its reason. An
exemption without a stated reason is indistinguishable from an oversight.

| Pattern | Reason |
|---|---|
| `**/archive/**`, `**/*-archive-*.md`, `**/*-superseded-*.md` | Destination of a roll cannot itself be over budget. |
| `CLOSEOUT-CANONICAL-CONTRACT.md` | Hash-bound; must stay byte-identical across repos (sentinel verified matching). |
| `platform/qt/avir/**` | Vendored upstream AVIR library. |
| `README.md` | Upstream MLV-App fork ancestry; diverging complicates merges. |
| `.claude/profiling/**`, `.claude/analysis/**`, `.claude-state/profiling/**` | Immutable recorded evidence; the profiling kits bundle copies of tracked docs, so governing them would double-count. |
| `.claude/worktrees/**`, `.claude-state/worktrees/**`, `.claude-state/closeout/repo-sweep/integration-probes/**` | Untracked full repo copies; would multiply every count. |
| `.claude-state/commit-message-rewrite/**` | Generated; regenerate rather than edit. |
| `.claude-state/llm-playback-*/**` | Packaged brief corpora; frozen inputs to an external consumer. |
| `**/*.bak` | Rotation leftovers, reaped by COORDINATION-PRUNE-POLICY. |

To bring an exempt group into scope: freeze existing files and govern only newly
created ones; for `CLOSEOUT-CANONICAL-CONTRACT.md`, the change must be agreed
across all repos sharing the sentinel and land in the same work block.

---

## Enforcement

| Mechanism | Status |
|---|---|
| `Stop` hook -> `tools/check-doc-size.py --warn-only --trace` (still the old copy: moving it to `tools/docs/run_doc_size.py` is an edit of `.claude/settings.json`, which CHECK-DOC-SIZE-SINGLE-SOURCE-1 r1 could not make - successor card) | **wired and OBSERVED FIRING** 2026-07-27T21:59:43Z; always exits 0 |
| `PostToolUse` hook -> `check_pinned_tokens.py --hook` | **wired and OBSERVED FIRING**; fails open on any input |
| Closeout audit harness gate | **deliberately not wired** |
| Pre-commit gate | **not touched** |

Both hooks are non-blocking by design. A hook that can fail a turn on doc hygiene
gets disabled within a week.

**Both are observed, not assumed - and the distinction cost a round to learn.** The
`Stop` hook originally ran with no side effects, which made "wired" and "inert"
indistinguishable after the fact; it was reported as enforcement on the strength of
configuration alone. `--trace` fixed that, and the hook then demonstrably fired at a
real turn boundary (a second record appeared in
`.claude-state/doc-size-runs.log` that no one invoked by hand). The `PostToolUse`
chain was proven separately, by throwing a real error when it briefly pointed at a
deleted file.

Verify either claim yourself rather than trusting this table:

```bash
cat .claude-state/doc-size-runs.log
```

A new record at each turn boundary means enforcement is live. **A configured hook is
not an enforced one until something it wrote proves it ran** - that is the general
rule, and it applies to any check this policy later adds.

**Why not the closeout harness.** `brokered_closeout.py` carries pinned assertion
counts and a `closeout_tooling_stale` blocker; adding a gate there would force a
manifest change plus a full proof cycle for a documentation check. A standalone
script plus hooks was the cheaper equivalent.

---

## Backlog

Recorded rather than freelanced, with reasons.

0. ~~**Verify the `Stop` hook fires in a real turn.**~~ **DONE 2026-07-27** - fired at
   `21:59:43Z`, 55 s after a hand-run baseline nobody repeated. Kept here rather than
   deleted: the item existed because the hook was unobservable, and the fix
   (`--trace`) is the reusable part.
1. **`.claude-state/` moves** - 7 `hub` breaches. Owner-executed under
   COORDINATION-PRUNE-POLICY P5/D1; blocked behind its Phase 1 (codex.md NUL
   repair) and an independent-lane byte-verify. Reported, not moved.
2. **`AGENTS.md`** (55,213 B, entry, breach) - same disease as `CLAUDE.md`:
   `## Brokered Auto-Closeout` is 32,819 B of it. The same split applies, but it
   carries 27 pinned tokens and is Codex's entry file, so it should be split with
   Codex in the loop rather than unilaterally.
3. **`claude/session-closeout.md`** (35,781 B) - under the 40 KB hard cap, over
   soft. A single 471-line prose block whose topic boundaries fall mid-paragraph;
   splitting further cannot be done verbatim by script without editorial
   judgement. Deferred rather than done badly.
4. **13 remaining `working` breaches** - `docs/` specs and `tools/agent-bridge/`
   plans. Mechanical, each needs its own verbatim demotion; none is auto-loaded,
   so none is urgent.
