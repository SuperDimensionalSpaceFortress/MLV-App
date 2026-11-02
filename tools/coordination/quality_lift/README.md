# Quality lift: shift the adversary left, then measure your own lift

Portable, project-neutral guidance. It names no project, host or board path and grants no runtime authority: the
Chief-of-Staff (CoS) stays advisory (bus RULINGS.md R18.1), and the adversary pass is not a review key.

| file | what it is |
|---|---|
| `adversary-pass-prompt.md` | Template for ONE read-only contract-attack pass on an exact local head: list the invariants, build accepted inputs that break each, cite file:line, sweep the class. Only blocker/major findings that the diff introduced are actionable; minor and pre-existing ones are advisories. |
| `Render-AdversaryPrompt.ps1` | Fills the template for one exact head in a single pass. A value that lands inside a command the reviewer runs (the worktree path, the `base...head` range) is rendered as a PowerShell single-quoted literal by the script, so `$name`, quotes, backticks and `;` in a path reach git unchanged. It refuses a blank or control-character path, a head that is not 40 lowercase hex, and a base that starts with `-`. |

No meter ships here. Classifying free-form CoS prose as blocked or clean did not converge over five review rounds, so
each project counts its own lift from its own review evidence (below) until the CoS writes a machine-readable verdict
line. A shared meter is proposed again only once that line exists.

## Run the adversary pass (producer lanes)

Before `gh pr create`, and before any push to an open PR:

1. Commit. Fill the template with `Render-AdversaryPrompt.ps1` for the exact local head (`git rev-parse HEAD`) and the diff base, and hand it to ONE
   read-only reviewer. A different provider family from the author is preferred. The same family is allowed when that
   provider is dark, because the provider choice is a preference and not a gate.
2. For each actionable finding: write a test that is RED on the head, fix it, and fix every sibling site the report
   lists (the class, not just the instance). Commit and rerun the pass. Stop after at most 2 reruns, and list anything
   still open in the PR body.
3. If the pass cannot run (no capacity, timeout), proceed and say `verdict=ERROR` in the PR body.

The PR body carries one line:

```
Adversary pass: <where the result is> verdict=<CLEAN|FINDINGS|ERROR> found=<n> fixed=<m>
```

On a push to an open PR, update that line in the PR body rather than adding a comment, so one place always holds the
current result.

## Measure your own lift

Count from your project's own review evidence: the review status contexts, native PR reviews or ledger entries your
factory leaves on a head. A commit status that says `success` is not a review on its own (CI says it too), so decide
first which evidence is a review approval, and write that down. For each PR head the CoS read, record the time of your
first review evidence and of the CoS pass, then count:

- **escaped:** your review keys approved the head, and the CoS later named a blocker on it;
- **factory-caught-first:** your review named the defect before the CoS read the head;
- **CoS-read-first:** the CoS read the head before any of your review evidence existed.

Also count the PRs whose body carries an `Adversary pass` line. When a CoS pass cannot be read as clearly blocked or
clearly clean, count it as unknown and report the unknown share beside every rate; never fold it into either side.

Record a 7-day baseline before you wire the pass in, and re-measure 48 h after. The targets, which are not merge
preconditions:

- first-round CoS blocked rate below 20 % over 24 h;
- CoS-read-first: 0;
- CoS rounds per PR at or below 1.3;
- an `Adversary pass` line on 100 % of new PRs.

When the CoS structured verdict line exists, read the verdict from that line instead of from the CoS prose.
