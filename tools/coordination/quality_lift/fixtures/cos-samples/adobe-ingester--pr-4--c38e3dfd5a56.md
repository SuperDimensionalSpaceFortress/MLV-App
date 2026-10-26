schema: cos-feedback.v1
project: adobe-ingester
repo: layibabalola/adobe-document-cloud-ingester
pr: 4
head: 23270dcc9113118048e722b64ad617e50b79445d
reviewed_utc: 2026-10-23T01:52:36Z
event: first-ready-review
verdict: ADVISORY
risk: HIGH-RISK (credential-equivalent session boundary and local document handling)
reviewer: primary dot
uuid: 864696e1-822b-4f03-a14c-2ad547ee5486
post_merge: false

# layibabalola/adobe-document-cloud-ingester #4 / 23270dcc9113118048e722b64ad617e50b79445d / ADVISORY

R18: non-gating advice, not merge/enqueue authorization or a factory review leg.

## Blockers
- B1 spikes/adobe-session/AdobeSessionSpike/Program.cs:115,1627,1724-1725: arbitrary observed paths now reach stdout even for Unknown routes. For example, `https://documentcloud.adobe.com/account/person/document/123` produces `route_path=/account/person/document/123`. This breaks README.md's explicit no-user-derived-path privacy guarantee and exposes account/document identifiers in retained probe evidence. Keep fixed allowlisted labels, or redact unknown paths before output. Add a path-sentinel regression; current tests:616-632 put sensitive sentinels only in query/fragment/user-info. Diagnostic intent in the PR body does not make those path values non-sensitive. No live account exposure was attempted or observed.

## Improvements
- Program.cs:1000-1015: unify check-export lexical validation with stricter import-export validation:1227-1247 and add parity cases for controls, non-ASCII drive letters and device aliases. Hardening only; no demonstrated bypass.
- README.md:74-89: update obsolete route-v1/selector-fallback descriptions to match route-v2/route-based marker-free behavior. New Acrobat paths remain explicitly unobserved hypotheses, not live-session proof.

## Nits
- None.

## Evidence and response duty
Source-first review covered all five changed files, including substantive copied PDF/session baseline and tests; lockfiles were mechanical. Compared same-tip reviews, inline threads and issue comments afterward: empty; no SHARED BLOCKER established. Live head rechecked, open/non-draft/CLEAN. Existing head checks/status contexts/workflow runs: zero; synthetic merge-tree runs: zero. Green required CI, independent Claude+Codex/HIGH-RISK third-key evidence and actual merged-tree CI are unverified, not waived. Authorship/test claims are not independent receipts. No tests or live Adobe actions launched. Author: ACK or DISTINGUISH within 24 hours; reserved-human/live-operation requirements remain.

— primary dot
