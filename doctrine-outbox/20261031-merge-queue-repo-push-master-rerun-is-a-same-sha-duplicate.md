---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/merge-queue-push-master-rerun-is-a-same-sha-duplicate
rule: where master takes only merge-queue commits (no bypass actors, enforce_admins on) and merge_group runs the same jobs and filters as push, a workflow's push:master trigger repeats the merge_group run of the same sha and, with cancel-in-progress, posts false failure contexts on master; run a required workflow on pull_request and merge_group only. Keep push for a workflow that has no merge_group run.
mechanism: tests.yml ran on push:master and merge_group for one sha; two close master pushes cancelled the older push run (39 of the last 100 push runs), and fail-closed `if: always()` aggregators then posted a failure context on a commit whose merge_group run was green. Every first-parent master commit since 2026-10-24 (67 on 2026-10-31) had a success merge_group run on its exact sha (the check command, run per `git log --first-parent` sha).
applies: mlv-app
check: the push run for landed master sha S is redundant only if `gh run list --workflow tests.yml --event merge_group --limit 400 --json databaseId,headSha,conclusion --jq '.[]|select(.headSha=="S" and .conclusion=="success")'` prints a row AND `gh run view <databaseId> --json jobs` shows every required job as success. Check-name presence or count on S proves nothing, because the push run posts the same names; names supplied only by push, only by workflow_dispatch, or by a failed merge_group run leave redundancy UNPROVEN. Count push cancels: `gh run list --workflow tests.yml --event push --branch master --limit 100 --json conclusion`.
supersedes: none
evidence: measured
