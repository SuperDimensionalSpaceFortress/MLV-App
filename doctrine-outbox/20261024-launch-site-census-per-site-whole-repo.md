---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/launch-site-census-per-site-whole-repo-1
rule: a census of the launch sites a live-reference guard must see classifies SITES, not files, and reads the whole repo. Every site that starts PowerShell from base64 or detaches a process, in any tracked *.ps1 / *.psm1 (tests and the guard excluded), is listed per site (path, a pinned substring of the launching statement, an expected count per file) with the reason the guard still sees it; a second site in a classified file, or a pinned site that moved or vanished, fails like a new file. The encoded-switch matcher is grammar-aware: it fires only in a pwsh argv context, covers -e, -ec, -enc, every prefix of -EncodedCommand and the `/` forms, and skips look-alikes (-Encoding, -ErrorAction, `git cat-file -e`, comments, prose). Its bound: PowerShell scripts only, Start-Process -WindowStyle Hidden / Win32_Process Create / [wmiclass] / Invoke-CimMethod Create after backtick continuations are joined, no launcher built from strings at run time.
mechanism: 2026-10-24 reviews of PR #665. The first census walked two roots and keyed a file-level found-set, so a second launch in a classified file kept the same set, and its single-line Start-Process pattern missed a hidden launch split over backtick continuations. The whole-repo walk newly surfaced the wake helper (encoded and detached, waited on with a timeout), the closeout dashboard, a build smoke probe and the venue probes; none starts a PowerShell driver that holds a lane worktree.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k launcher_census (42 cases; 15 were RED before the fix).
supersedes: 20261024-worktree-guard-unknown-holder-and-encoded-driver (its census sentence said "every tracked launch site" over two roots)
evidence: measured
