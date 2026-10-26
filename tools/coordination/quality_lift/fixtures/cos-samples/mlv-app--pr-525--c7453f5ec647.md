schema: cos-feedback.v1
project: mlv-app
repo: SuperDimensionalSpaceFortress/MLV-App
pr: 525
head: c831ed5f348472fc0695dc003b8d5a119510f4e3
reviewed_utc: 2026-10-20T03:51:39Z
event: review-requested
verdict: not-merge-ready
ci_summary: 29/29 hosted check runs successful; exact-head independent review/opus and review/sol successful.
reviewer: dot
seat: primary
review_uuid: 180e287f-a526-4dc7-b8b1-efb77cc75d02
seat_resolution_id: 38b4ccf8-603d-45d4-b2c2-24b909773a6b
risk: ordinary (venue orchestration; classification uncertainty retained)
provisional: false
asks:
  - "ACK: plan or ETA for queue-aware supplementary claim acquisition and deterministic interleaving coverage."

ADVISORY — SuperDimensionalSpaceFortress/MLV-App #525 / c831ed5f348472fc0695dc003b8d5a119510f4e3
## Blockers
P2, high confidence: tools/profiling/dual-venue/Invoke-VenueChain.ps1:312-320 checks idle/no-live-ticket before a potentially lengthy manifest read, then requests a ticketless claim. A normal UM waiter can create a ticket meanwhile and be descheduled before claiming. Supplementary then atomically takes the free claim ahead of it: VenueChain.psm1:368-370 bypasses ticket arbitration; :177-181 checks only claim file. This violates the no-live-ticket prerequisite and can defer primary work for a whole supplementary leg. Use one-shot queue-aware acquisition or revalidate/yield while owning the claim before work, with deterministic interleaving coverage. A second unlocked check alone leaves a race.
## Improvements
Invoke-VenueChain.ps1:337-355; VenueChain.psm1:610-614: timeout kills local client, not an already submitted remote job. Preserve remote identity/reconciliation; released claim does not prove idleness. Existing quiet/agent gates limit demonstrated consequence; no overlapping-playback blocker asserted.
VenueChain.psm1:583-593: validate advertised cache-read bound for stalled UNC operations; asynchronous stop followed by synchronous disposal needs targeted coverage.
## Nits
None.
## Notes
Source-first pass before current reviews, earlier draft classification disclosed. Opt-out, separate supplementary results, fail-closed UNKNOWN and prior admission repairs intact. Weights mechanical, code separately reviewed. No execution.
Packet https://github.com/SuperDimensionalSpaceFortress/MLV-App/pull/525#issuecomment-6282434762 verifies distinct nonediting Opus/Sol r4, separate from four Sonnet implementing runs. Complete report hashes recomputed and successful status prefixes f9f07695c99a7fd3 / 2504104e59305fbd matched. Both approve and defer queue fairness; this advisory disagrees on severity, not SHARED BLOCKER. Deferral does not fix the defect.
## Merge gates
Fix queue defect and refresh exact-head evidence. Own advisory is not a factory leg. Preserve CI/ACK/reserved-human gates; no rendering/golden change or invented real-clip A/B requirement.
