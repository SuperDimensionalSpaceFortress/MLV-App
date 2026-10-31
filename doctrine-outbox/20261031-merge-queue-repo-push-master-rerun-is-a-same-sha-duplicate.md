---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-31 — on a merge-queue repo a push-to-master re-run of the same workflow is a same-sha duplicate, and with cancel-in-progress it puts false failure contexts on master

**Trap (a), the duplicate:** a workflow that triggers on both `merge_group` and `push: branches: [master]` runs twice for one commit. In a merge-queue repo every master commit is the merge-group commit, so the check suite of the `merge_group` run already attaches to the landed master sha, with every required check name. The `push` run adds no coverage for that sha. It is a second copy of the same suite.

**Measured (mlv-app, tests.yml, 29 check runs per run, 5 required names):** 70 of 70 first-parent master commits since 2026-10-24 had the full suite attached to the exact sha from their merge-group run. The push re-run cost about 3,469 hosted Windows minutes per 24 h.

**Trap (b), false failures:** the workflow used `cancel-in-progress` keyed on the ref. Two master pushes close together cancelled the older push run. 39 of the last 100 push runs were cancelled. Fail-closed aggregator jobs (`if: always()` jobs that fail when any needed job is not `success`) turn a cancelled shard into a posted `failure` context on the older master commit. That red context sits on a commit whose merge-group run was green. It was verified on a merge commit of this repo on 2026-10-31.

**Rule:** on a merge-queue repo, a required workflow runs on `pull_request` and `merge_group` only. A `push: master` trigger of the same workflow is a duplicate, and a cancelled duplicate is a false red. Keep `push` only for a workflow that has no merge-group run, such as a release job.

**Check it (no host access needed):** pick a master commit, run `gh api repos/<owner>/<repo>/commits/<sha>/check-runs --jq '[.check_runs[].name]|length'` and compare with the names the ruleset requires. All present means the push run was redundant. Count `gh run list --workflow <file> --event push --branch master --limit 100 --json conclusion` for `cancelled`.

**Fix (mlv-app card CI-PUSH-MASTER-DEDUP-1):** delete only the three trigger lines for `push`. No job, name, timeout, concurrency group or required check changes. The test that pinned the push trigger now pins `merge_group`.

**Falsifier:** after the change lands, 20 consecutive master commits must each show the full check suite from a `merge_group` run on the exact sha, zero `push` runs of that workflow, and no failure context on any of them. A master commit with a required name missing, or a failure context present, means this trap was wrongly applied.
