schema: cos-feedback.v1
project: mlv-app
repo: SuperDimensionalSpaceFortress/MLV-App
pr: 465
head: 37b6e55bafed02078740512f8c07a3f966d8f369
reviewed_utc: 2026-10-19T07:47:14Z
event: head-changed
verdict: not-merge-ready
ci_summary: Hosted Windows shard1 and Ubuntu shard3 fail measured-weight guard; current-head required producer/pair evidence absent.
reviewer: dot
seat: primary
review_uuid: 5e2142d4-a33a-4c7c-a97a-42a2598de301
seat_resolution_id: primary-seat-20261019T073209Z-intake-0715
risk: HIGH-RISK (NA-2 containment lint)
provisional: true

ADVISORY — SuperDimensionalSpaceFortress/MLV-App#465
Head: 37b6e55bafed02078740512f8c07a3f966d8f369

## Blockers
- tools/repo_hygiene/test_ci_unittest_shard.py:181: actual [Windows shard 1](https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39215746963/job/117939063420) and [Ubuntu shard 3](https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39215746963/job/117939063389) fail: 227 tests in 42 classes lack measured weights; share 0.0516026 exceeds 0.05. Refresh measured data and restore green CI without weakening the guard. This is a verified CI hold, not a demonstrated lint bypass.
- Current-head completed producer plus independent Sol/Claude finals/statuses are absent. Old r14c evidence at 8beddf2b is stale. No SHARED BLOCKER.

## Improvements
- safety_lint_parity.py:103-121,647-657: 11,807 rows from 24 exact-name HEAD tuples are excluded, but baseline copies always rejoin. After merge, the expensive baseline union returns; the speed follow-up must cover that lifecycle.
- safety_lint_parity.py:109 and test_hub_vm_no_local_playback.py:1086-1101: the alternative differential lacks some R8/R9/full-fuzz families. Direct refuse/mutation tests remain; preserve an explicit coverage audit rather than imply one-for-one parity.

## Nits
None new. runtime-floors.json:50 now correctly cites lint line128.

## Evidence and disposition
Independent delta/prior-finding disposition saved 07:44:02Z before current threads/feedback. Main lint blob remains identical to reviewed 8beddf2b; this round changes parity exclusions/pins and a citation after master merges. Baseline preservation/exact-name behavior is pinned. No new bypass independently established.

Actual CI checkout 0c277409 merges this head into 16478851; failures are current. Earlier culture/restoration/cache/selector and sentence/code-span follow-ups remain nonblocking, as recorded in the [prior review](https://github.com/layibabalola/softwarefactory-fleet-doctrine/blob/f2aefbb609a2c01b15c3fbcfeac2b4d9b6968ef6/cos-feedback/mlv-app/pr-465.md).

ACK6245785855 applies to the older head. Plan6260830707 acknowledges integration stales keys. Hub code comments about a ruling are not owner clearance. Actual containment-lint HIGH-RISK remains provisional under Amendment 5: tally1/3; exact-head clearance still needed after evidence/CI closes. No duplicate owner ask. Dot supplies no factory leg; no tests or merges performed.
