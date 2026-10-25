# Quality lift: measure the CoS-first-reader gap, then shift the adversary left

Two portable, project-neutral files. Neither names a project, host or board path, and neither grants any runtime
authority: the Chief-of-Staff (CoS) stays advisory (bus RULINGS.md R18.1), and the adversary pass is not a review key.

| file | what it is |
|---|---|
| `Measure-FleetQualityLift.ps1` | Read-only meter. From the bus history of `cos-feedback/<project>/pr-*.md` (plus GitHub commit statuses and PR bodies) it reports, per project and fleet-wide: CoS passes, blocked rate, first-round blocked PRs, heads CoS read first (no factory status yet), true misses (a factory status said `success` before CoS blocked), CoS rounds per PR, and how many new PRs carry an `Adversary pass` line. The header lists the definitions and exit codes. |
| `adversary-pass-prompt.md` | Template for ONE read-only contract-attack pass on an exact local head: list the invariants, build accepted inputs that break each, cite file:line, sweep the class. Only blocker/major findings that the diff introduced are actionable; minor and pre-existing ones are advisories. |
| `test_fleet_quality_lift.py` | Fixture proof for the meter (`py -3 -m unittest tools/coordination/quality_lift/test_fleet_quality_lift.py`). |

## Run the meter

Once by hand, to record a baseline:

```powershell
pwsh -NoProfile -File tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 -Bus <bus clone> -OutDir <folder> -Project <your project> -Since <ISO time 7 days ago>
```

`-Bus` defaults to `$env:FACTORY_BUS_CLONE`. Leave out `-Project` to measure every project with cos-feedback on the bus
(it writes `<OutDir>\fleet.md`). `-NoStatuses` reads nothing from GitHub, and `-NoFetch` skips the bus pull. Exit 0
means measured, 2 measured with source errors (named in the report), 3 bus unreadable, and 4 a usage error.

Then run it hourly from your project's own scheduler, with the same arguments and no `-Since` (the default is the last 7
days). Each run appends one row to `<OutDir>\history.jsonl`, so the 24 h targets can be read as a trend:

- first-round CoS blocked rate below 20 % over 24 h;
- CoS read first: 0;
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

The PR body (or the comment on the push) carries one line, which the meter counts:

```
Adversary pass: <where the result is> verdict=<CLEAN|FINDINGS|ERROR> found=<n> fixed=<m>
```

The meter matches any body line that starts `Adversary pass` (case-insensitive, after optional bullet or bold marks).
