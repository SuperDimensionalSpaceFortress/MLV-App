---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-23 — a deleted worktree admin directory leaves the working files intact but leaves git blind for an entire overnight run: no commits, no diffs, no log

**Symptom (measured):** a git worktree had been "git-blind" for an entire multi-hour autonomous run (an overnight optimization pass). The `.git/worktrees/<name>/` admin directory backing that worktree had been deleted at some point before the run started. No matching `git worktree remove`/`prune`/`move`, `Remove-Item`, or `Rename-Item` command appeared anywhere in shell history — the most likely causes were an out-of-band cleanup, a sync/antivirus quarantine action, or a move/copy-and-cleanup workflow touching `.git` internals it shouldn't have. The working directory itself survived untouched, with its `.git` file still pointing at the now-missing admin path, so every git command failed with `fatal: not a git repository`, but ordinary file edits succeeded silently and looked completely normal to anyone not running git commands.

**Cause:** a git worktree's actual repository state lives partly outside the worktree directory itself, in the main repo's `.git/worktrees/<name>/` admin folder. Anything that deletes or moves that admin folder — without going through `git worktree remove`/`prune`/`move` — orphans the worktree: the files stay, the git plumbing does not, and there is no error until something tries to use git in that directory.

**Rule:** before starting any long unattended run inside a worktree, verify it end-to-end: `git worktree list` from the main repo must show the target path, AND `git status` run *inside* the worktree must succeed (not just "the path is listed"). If either check fails, stop and repair before proceeding — recovery requires creating a fresh, properly-registered worktree and transplanting file contents (e.g. via a file copy excluding `.git`), which discards fine-grained commit history for anything done since the corruption. Commit early and often during long runs so file-state, at minimum, is recoverable even if history isn't.

**Falsifier (measured 2026-09-29, disposable repo, admin folder moved out of `.git/worktrees/`):** from the main repo, `git worktree list` no longer shows the worktree at all, and `git status` inside it fails with `fatal: not a git repository: <main>/.git/worktrees/<name>` (exit 128) while its files are untouched. So the two checks in the Rule catch it from opposite sides: the first by the worktree's absence, the second by the failure. (Moving the folder to a new name *inside* `.git/worktrees/` does not reproduce it: git still enumerates that folder.)
