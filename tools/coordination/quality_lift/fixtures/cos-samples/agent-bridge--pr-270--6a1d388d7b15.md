schema: cos-feedback.v1
project: agent-bridge
repo: layibabalola/agent-bridge
pr: 270
head: 5db5a5ea6d950df2d4804e6a617f788217f6ceae
reviewed_utc: 2026-10-25T04:28:45Z
event: opened
verdict: ADVISORY
ci_summary: Windows CI40161918840 queued
reviewer: primary dot
review_uuid: b67fe852-c1e7-4beb-b2cd-2c414eb34f90
posture: non-gating ADVISORY under R18

# layibabalola/agent-bridge #270 / 5db5a5ea6d950df2d4804e6a617f788217f6ceae / ADVISORY
## Blockers
Inherited ownership B1 remains at dashboard_launcher.py:363-373: reading matching PID/URL and later unconditionally replacing the record is not atomic with another launcher's publication. A reads its record; B publishes a live record; A overwrites it with stopped A. New exception cleanup does not repair this interleaving. Prior267/268 asks and one overlapping reminder remain unanswered; no duplicate outreach or reset.
## Improvements
test_agent_bridge.py:8610,8632,8647,8669,8705,8721: all four new tests assume chmod(read-only destination) makes sibling replacement fail. On ordinary POSIX with writable parent, dashboard_launcher.py:66-70 replaces that file successfully. T1/T2 then have no diagnostic, T3 never invokes patched unlink, and T4 changes the record. These are unguarded AgentBridgeTests methods. Inject the failure after temp creation for portable unit coverage and explicitly scope any real read-only-file integration case to Windows. Source-proven portability issue, not an observed run failure or claim required Windows CI is red.
dashboard_launcher.py:374-380 correctly limits best-effort cleanup to this PID's named sibling and preserves the existing guarded fd2 diagnostic. Other-PID and unrelated files are not targeted.
## Nits
No separate new nit.
## Coverage and disposition
Complete two-file source-first review; prior own ownership finding exposure disclosed. Independent source-only test pass confirmed POSIX reasoning, without execution. No same-tip external feedback at comparison; inherited Grok agreement is prior evidence, not a new same-tip independent key. Producer RED/mutant claims remain attributed.
Windows40161918840 queued. Exact-head independent Claude+Codex, any required third key and actual merged-tree CI unverified; footer is not provenance proof. No product/testing actions. R18 advisory is not merge clearance; preserve all factory legs, owner reservations and denials.
Signed: primary dot.
