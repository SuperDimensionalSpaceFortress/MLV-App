---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-27 — `cd`-ing to the main checkout to run a git mutation lands the commit on whatever branch that checkout has, not the worktree you were working in

**Symptom (measured):** while operating inside a git worktree checked out on a feature branch, a commit was made by running `cd <main-repo-folder> && git add <file> && git commit -m ...`. The commit landed on the main checkout's branch (its default long-lived branch), not the feature branch whose code the new file actually documented. The commit was functionally useless there (it referenced files that didn't exist on that branch) and advanced that branch one commit ahead of its intended upstream state for no reason.

**Cause:** in a multi-worktree repository, each worktree (including the "main" one) has its own independently checked-out branch. `cd <path> && git <mutating-command>` looks innocuous — the working directory is just "a path" — but in a worktree-rich repo the path silently *determines the branch* a mutation lands on. The failure is invisible at commit time unless the result line (`[<branch> <hash>] <subject>`) is actually read.

**Rule:** never `cd` out of the worktree you are actively operating in to run a git mutation (`add`/`commit`/`reset`/`checkout`/`merge`/`cherry-pick`/`rebase`). If another worktree's branch genuinely needs a change, use `git -C <path> <command>` so the branch target is explicit in the command text and reviewable, and treat it as a deliberate cross-branch action rather than an incidental `cd`. After every commit, read the `[<branch> <hash>]` line and confirm the branch name matches what you intended — this is the cheap, mechanical check that catches the mistake before it needs a cherry-pick recovery.

**Falsifier:** in a worktree checked out on branch A, run a mutating git command after `cd`-ing to a sibling checkout on branch B, and confirm the result reports branch B; recovery when caught unpushed is `git cherry-pick <hash>` onto the intended branch followed by resetting the wrong branch to its prior upstream state.
