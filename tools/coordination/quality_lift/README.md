# Quality lift: measure the CoS-first-reader gap, then shift the adversary left

Two portable, project-neutral files. Neither names a project, host or board path, and neither grants any runtime
authority. The per-project values (which evidence is a project's review) live only in `review-sources.json`: the Chief-of-Staff (CoS) stays advisory (bus RULINGS.md R18.1), and the adversary pass is not a review key.

| file | what it is |
|---|---|
| `Measure-FleetQualityLift.ps1` | Read-only meter. From the bus history of `cos-feedback/<project>/pr-*.md` (plus GitHub commit statuses, native PR reviews where declared, and PR bodies) it reports, per project and fleet-wide: CoS passes, blocked rate, first-round blocked PRs, heads CoS read first (no declared review evidence before CoS), true misses (a declared approval before CoS blocked), the raw status proxy (`statusBeforeCos`), CoS rounds per PR, and how many new PRs carry an `Adversary pass` line. The header lists the definitions and exit codes. |
| `review-sources.json` | Which evidence each project's factory review leaves (`-ReviewSources`). "CoS read first" and true misses are claimed only from it; a project it does not name reads `unknown`. |
| `adversary-pass-prompt.md` | Template for ONE read-only contract-attack pass on an exact local head: list the invariants, build accepted inputs that break each, cite file:line, sweep the class. Only blocker/major findings that the diff introduced are actionable; minor and pre-existing ones are advisories. |
| `test_fleet_quality_lift.py` | Fixture proof for the meter (`py -3 -m unittest tools/coordination/quality_lift/test_fleet_quality_lift.py`). |
| `fixtures/cos-samples/` | Real CoS records copied verbatim from the bus, named `<project>--pr-<n>--<bus commit>.md`. With `FACTORY_BUS_CLONE` set, a test checks each one against the bus at that commit. |

## Run the meter

Once by hand, to record a baseline:

```powershell
pwsh -NoProfile -File tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 -Bus <bus clone> -OutDir <folder> -Project <your project> -Since <ISO time 7 days ago>
```

`-Bus` defaults to `$env:FACTORY_BUS_CLONE`. Leave out `-Project` to measure every project with cos-feedback on the bus
(it writes `<OutDir>\fleet.md`). The meter writes only under `-OutDir`, except one `git pull --ff-only` of the bus
before it measures; `-NoFetch` skips that pull, and a pull that fails is a named source error. `-NoStatuses` reads
nothing from GitHub: blocked heads with no cached statuses then count as "unknown", never as "CoS read first". Exit 0
means measured, 2 measured with source errors (named in the report), 3 bus unreadable, and 4 a usage error. A project
whose bus history cannot be read, or whose lock another run holds, is reported as **not measured**: its report says so,
it adds no history row, and the fleet table names it and leaves it out of the fleet totals. When a PR listing fails, the
adoption count is unknown (null), never a partial sum.

`corpus.jsonl` holds the blocked heads with known evidence. When the last row of a head has another evidence basis (the
exact declaration, `reviewBasis`, and the row format, `evidence`), a new row with `supersedes: true` is appended, so read
the last row per repo@head. Rows are never rewritten. `-Since` must carry an offset or `Z`; a bare date is a usage error.

## What the meter reads from a CoS file

- **The pass:** the last `# <owner>/<repo> #<n> / <40-hex head>` heading, or else the `cos-feedback.v1` front matter
  (`repo:`, `pr:` and `head:` in the leading `schema: cos-feedback.v1` block). A file with neither is counted as
  "names no pass" in the report, never silently dropped.
- **Blocked:** the first content line of the Blockers section starts `B<n>`. The section is a `## Blockers` or
  `### Blockers` heading (bold, a colon inside or after the bold, a note in `()` or `[]` allowed) or a `Blockers:` label
  line. The line may carry quote, list (`- * +`, `1.`, `1)`), `###` heading or one to three `*`/`_` marks before `B<n>`.
  A head written in capitals is read as the same head in lowercase. A line under the next level-1
  or level-2 heading is outside the section, so `## Blockers` followed by `## Improvements` and `- B1 ...` is clean.
  `None demonstrated.`, `- None.` and `Prior B1 is fixed.` are clean.

## Declare your review evidence

A commit status that says `success` is not a review: CI says it too. So the meter claims "CoS read first" and true misses
only from evidence your project declares in `review-sources.json` (or the file you pass as `-ReviewSources`):

```json
{"schema": "fleet/quality-lift/review-sources/v1",
 "projects": {"<project>": {"statusContexts": ["review/*"], "nativeReviews": true, "reviewAuthors": ["<login>"]}}}
```

- `statusContexts`: PowerShell wildcards naming your factory's review status contexts. Any state of a matching context
  before CoS is prior review; its `success` is an approval.
- `nativeReviews`: also read GitHub PR reviews on that head (`commit_id`) and review comments written on it
  (`original_commit_id`), optionally only by `reviewAuthors`. Only an `APPROVED` review is an approval; any other review
  and every review comment is prior review evidence, never an approval. GitHub returns a dismissed review as `DISMISSED`
  with its original `submitted_at`, so the meter reads the PR's `review_dismissed` issue events for the state it was
  submitted with: a dismissed approval is still an approval at its submission time. When that event cannot be found, the
  head reads `unknown`, never "no approval".

Declare at least one. A project the file does not name reports `unknown` for CoS read first and true misses, and the
fleet totals leave it out. Its raw status counts stay in `statusBeforeCos` (no status of any context before CoS; some
context said `success` before CoS), a proxy that proves neither a review nor its absence.

Then run it hourly from your project's own scheduler, with the same arguments and no `-Since` (the default is the last 7
days). Runs on one project folder take turns through a lock file there; a run that waits past `-LockWaitSec` (default
60) writes nothing and exits 2. Each run appends one row to `<OutDir>\history.jsonl`, so the 24 h targets can be read as a trend:

- first-round CoS blocked rate below 20 % over 24 h;
- CoS read first: 0 (from declared review evidence only);
- CoS rounds per PR at or below 1.3;
- an `Adversary pass` line on 100 % of new PRs.

## Run the adversary pass (producer lanes)

Before `gh pr create`, and before any push to an open PR:

1. Commit. Fill the template for the exact local head (`git rev-parse HEAD`) and the diff base, and hand it to ONE
   read-only reviewer. A different provider family from the author is preferred. The same family is allowed when that
   provider is dark, because the provider choice is a preference and not a gate.
2. For each actionable finding: write a test that is RED on the head, fix it, and fix every sibling site the report
   lists (the class, not just the instance). Commit and rerun the pass. Stop after at most 2 reruns, and list anything
   still open in the PR body.
3. If the pass cannot run (no capacity, timeout), proceed and say `verdict=ERROR` in the PR body.

The PR body carries one line, which the meter counts:

```
Adversary pass: <where the result is> verdict=<CLEAN|FINDINGS|ERROR> found=<n> fixed=<m>
```

On a push to an open PR, update that line in the PR body: the meter reads PR bodies only, never comments. It matches
any body line that starts `Adversary pass` (case-insensitive, after optional bullet or bold marks).
