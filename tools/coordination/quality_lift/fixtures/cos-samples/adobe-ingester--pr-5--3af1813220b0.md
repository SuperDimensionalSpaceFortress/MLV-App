schema: cos-feedback.v1
project: adobe-ingester
repo: layibabalola/adobe-document-cloud-ingester
pr: 5
head: ea8cf4558da328b4c37db505e55938d9389e1baf
reviewed_utc: 2026-10-24T01:50:20Z
event: first-ready-review
verdict: ADVISORY
reviewer: primary dot
review_uuid: 41b68362-83b0-414b-9b8e-ad72616b48d6
posture: non-gating ADVISORY under R18

# layibabalola/adobe-document-cloud-ingester #5 / ea8cf4558da328b4c37db505e55938d9389e1baf / ADVISORY
## Blockers
Inherited #4 B1 remains: spikes/adobe-session/AdobeSessionSpike/Program.cs:115,1644,1760-1761 still copies arbitrary observed AbsolutePath to stdout. A path /account/person/document/123 exposes identifiers even when classification is Unknown, contrary to spikes/adobe-session/README.md's explicit no-user-derived-path/evidence guarantee. Route-v3 does not redact it. Use fixed allowlisted labels/redaction and a path-sentinel regression. No live account exposure attempted/observed. No matching independent blocker observed; not SHARED BLOCKER.
## Improvements
Program.cs:1854,1881:15s settle deadline is checked only after unbounded EvaluateAsync returns; the caller's cancellation token is not passed to that await. Use a remaining-budget/cancellation-bound await plus loop tests for stalled evaluation and deadline. Static timeout hardening; no live stall reproduced. Post-pass Grok6378189925 independently matches this concern.
## Nits
README route-v1/selector fallback descriptions remain obsolete under route-v3.
## Coverage and readiness
Complete two-file code/test source-first review. New AcrobatRoot uses IMS only when marker absent; recognized DOM marker retains precedence. Fixed IMS labels avoid dumping IMS data. Source comments claim an owner observation; not independently verified here. Post-pass Grok also raises initial SignedOut, strict boolean and URL-snapshot hardening; these are attributed, not independently demonstrated new blockers.
Live open/non-draft/CLEAN; zero checks/statuses reported. No verified exact-head independent Claude+Codex, HIGH-RISK third key or actual merged-tree evidence; authored test counts are not independent proof. No tests, browsers or live Adobe actions launched.
R18 advisory is not merge authorization or a CoS gate. Preserve all factory keys/CI/third-key, live-operation and reserved-human requirements. ACK/DISTINGUISH within24h citing UUID; prior #4 duty not reset.
Signed: primary dot.
