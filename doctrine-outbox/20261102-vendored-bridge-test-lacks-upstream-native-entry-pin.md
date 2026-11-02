---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin
rule: a test that mocks subprocess.run but not the native fast path before it depends on the host's process table; Windows opens a pid with its low two bits ignored, so a made-up pid 789 can reach a live pid 788. Pin native_process_entry to None, assert run.called before reading call_args, never skip or deselect the test.
mechanism: test_server_wrapper_process_entry_prefers_pwsh_for_cim (tools/agent-bridge/test_server_wrapper_phase2.py:859) failed on the hosted runner at :872, AttributeError 'NoneType' has no attribute 'args' (a fresh 14-test unittest step, 2.4 s), and passed on rerun. server.py:295 _wrapper_process_entry returns at :301 when core/win_process.py:314 native_process_entry answers, so the mock is never called. Native lookup answers for a live pid and live pid+1..+3; forcing the test body onto a live pid and onto live pid+1 fails the unpinned body 2 of 2 and passes the pinned body 2 of 2. Unforced: 40 of 40 pass. Upstream pinned this test in cccf7b4b (PR 2, 2026-09-07) and its trampoline sibling in cf4a9964 (PR 22); the in-tree copy has no mention of native_process_entry. The hosted pid is a hypothesis; the log prints no pids.
applies: mlv-app
check: `git grep -c native_process_entry fork/master -- tools/agent-bridge/test_server_wrapper_phase2.py` prints nothing while the same grep in a layibabalola/agent-bridge clone on origin/master prints 5 or more; a resync or retirement of the in-tree test ends the difference. Guard: test_server_wrapper_phase2.py::ServerWrapperPhase2Tests::test_server_wrapper_process_entry_prefers_pwsh_for_cim in layibabalola/agent-bridge.
supersedes: none
evidence: measured
