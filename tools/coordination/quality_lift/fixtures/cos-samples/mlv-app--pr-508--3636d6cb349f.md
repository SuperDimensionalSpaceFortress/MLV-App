schema: cos-feedback.v1
project: mlv-app
repo: SuperDimensionalSpaceFortress/MLV-App
pr: 508
head: 24fdc1986e94655da05adab911904ceb811893a7
reviewed_utc: 2026-10-19T07:47:14Z
event: head-changed
verdict: not-merge-ready
ci_summary: 27 checks pass, Windows shard2 and aggregate fail; current-head independent keys absent after mechanical merge.
reviewer: dot
seat: primary
review_uuid: 6c9c6c11-2f87-437c-97a7-f5adbfe566a4
seat_resolution_id: primary-seat-20261019T073209Z-intake-0715
risk: ordinary LOW-CONFIDENCE (venue tooling)
provisional: false

ADVISORY — SuperDimensionalSpaceFortress/MLV-App#508
Head: 24fdc1986e94655da05adab911904ceb811893a7

## Blockers
- tools/repo_hygiene/test_dual_venue_git_calls.py:193: actual [Windows shard 2](https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39212359277/job/117940494767) fails at 07:42:20Z: expected "(killed)", received DV_GIT_PIPE_STRAGGLER with "(kill not confirmed)". Typed failure remains; successful child termination is unproven. Investigate and restore required CI. The test blob is identical to master16478851, inherited from #504; attribution to #508's wait change is not established.
- Current-head completed producer and independent approving pair are absent; previous acc44241 keys and readiness cannot transfer. No SHARED BLOCKER.

## Improvements
Prior findings remain: test_dual_venue_evidence.py:1406-1429 starts repair before startup, weakening discrimination; DualVenueRunner.psm1:141-154 does not bound synchronous network/hash I/O; uppercase hash acceptance/case-sensitive comparison fails closed; Invoke-VenueLeg.ps1:500-516 observation retry is not restaging.

## Nits
Prior docs/dual-venue-evidence.md:430-435 diagnostic punctuation/state-prefix mismatch remains.

## Evidence and disposition
Mechanical source SKIP verified before current threads: merge of master16478851 plus runtime-floors.json:83 citation2198→2212, matching the live literal. Invoke-VenueLeg.ps1, evidence tests and documentation blobs are byte-identical to acc44241; wait logic is unchanged. No repeated substantive review.

Current [Windows shard 3](https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39212359277/job/117940494700) passes the staged-manifest settle/refusal rows, 394 tests. Both logs use synthetic854e1a42 merging this head into16478851. All29 visible checks completed:27success/2failure.

[Plan6261359491](https://github.com/SuperDimensionalSpaceFortress/MLV-App/pull/508#issuecomment-6261359491) explicitly stales r2 evidence and requests refresh after green CI/new keys. Earlier ACK6260027571 accepts nonblocking follow-ups. [Prior record](https://github.com/layibabalola/softwarefactory-fleet-doctrine/blob/f2aefbb609a2c01b15c3fbcfeac2b4d9b6968ef6/cos-feedback/mlv-app/pr-508.md).

Ordinary venue tooling, LOW-CONFIDENCE; no new reserved-human category established. Dot supplies no factory leg. Administrative-protection403 remains uncertified/unretried. No execution or merge authority.
