schema: cos-feedback.v1
project: mlv-app
repo: SuperDimensionalSpaceFortress/MLV-App
pr: 671
head: 06ebdd1903e8474d385fe504323f4c0358aeca06
reviewed_utc: 2026-10-25T08:32:32Z
event: material-ci-reconciliation
verdict: ADVISORY
ci_summary: Exact-head Bridge attempt2, Release, Canary and Tests successful; prior flake remains follow-up
reviewer: primary dot
review_uuid: 4fa77808-2ea8-4ab2-841b-e1b736ba2d68
posture: non-gating ADVISORY under R18

# SuperDimensionalSpaceFortress/MLV-App #671 / 06ebdd1903e8474d385fe504323f4c0358aeca06 / ADVISORY

## Blockers
No current failing check demonstrated. Prior B1 was an actual failure of Bridge attempt1/job121109804054 at07:42:37 (missing subprocess call_args at wrapper test:872). Same exact-head attempt2/job121117036688 succeeded08:22:47. This resolves the current red-check observation, not the unverified fixture/native-fast-path cause or its recurrence risk. Factory ACK6395340796 accepts investigation and a source-of-truth remedy card; no source fix/card completion verified. No dot rerun or unchanged-code rereview.
https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/40173706411

## Improvements
Prior release Qt-major concern source-addressed: .github/workflows/macOS-Arm64.yml:38–40 rejects non-6 versions before payload/build; mutation guards at test_vendored_native_payloads.py:1918–1923. Top-level stale architecture blocker removed from tools/gates/vendored-native-payloads.json:45–54; other redistribution restrictions retained. Swap-back regression at test_vendored_native_payloads.py:299–315 now aligns payload sets and expects architecture rejection, rather than earlier set mismatch.

DMG runtime/rpath completeness remains unproven; unchanged packaging/evidence steps .github/workflows/macOS-Arm64.yml:70–85 add no resolution/launch validation.

## Nits
Manifest:209 still says blocked-missing-notices-and-arm64-payload although Arm64 uses its own payload. Remove only stale architecture wording, preserving missing-notices restrictions.

## Scope and evidence
One linear commit, parented directly to ed49ac51a94c210683cbe21533b2cce602be6b7a; three owned files, no imports. Source-first pass preceded same-tip feedback; historical exposure disclosed. No execution. Exact-head Release40173708044, Canary40173706415 and Tests40173706423 succeeded. Same-head rerun success is not proof of durable fixture repair. No new-tip review receipts found; r5 keys/green do not transfer.

R18 advisory is not merge authorization. Independent pair, applicable third key, required CI/actual merged-tree evidence and owner .github approval remain separately required; all denials/reserved-human limits retained.

Signed: primary dot.
