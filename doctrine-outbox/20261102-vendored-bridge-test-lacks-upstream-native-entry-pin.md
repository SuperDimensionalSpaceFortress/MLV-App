---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin
rule: a test that mocks subprocess.run but not the native fast path before it depends on the host's process table (Windows ignores a pid's low two bits, so 789 can reach a live 788). Pin native_process_entry to None; never skip or deselect the test.
mechanism: test_server_wrapper_process_entry_prefers_pwsh_for_cim (test_server_wrapper_phase2.py:859) failed on the hosted runner with AttributeError on call_args=None, then passed on rerun. server.py:302 returns when win_process.py:314 native_process_entry answers, so the mock is never called. Forced onto a live pid and live pid+1: unpinned fails 2 of 2, pinned passes 2 of 2; unforced 40 of 40 pass. Upstream pinned it in cccf7b4b (PR 2), its trampoline sibling in cf4a9964 (PR 22); the in-tree copy has no pin. The hosted pid is a hypothesis.
applies: mlv-app
check: `py -3 tools/coordination/check_bridge_native_pin.py fork/master` RUNS the vendored test with the native fast path forced to a live pid (needs tools/agent-bridge/requirements.txt installed; GIT_TRACE* scrubbed). The test runs under CI's package identity (tools.agent-bridge.test_server_wrapper_phase2). The forcing is written into the extracted core/win_process.py, so reload or a direct file load cannot undo it, and the forced entry counts its own calls: any call is UNPINNED. A sys.monitoring observer (Python 3.12+, all threads) watches every frame of server._wrapper_process_entry and flags one returning its native result; no line numbers. Prints one line: UNPINNED now (exit 1); fixed only when PINNED (exit 0). A skipped, shadowed, unreachable, overridden or aliased-around pin, an absent test or any error is UNKNOWN or UNPINNED, never PINNED. Guard: test_server_wrapper_phase2.py::ServerWrapperPhase2Tests::test_server_wrapper_process_entry_prefers_pwsh_for_cim in layibabalola/agent-bridge.
supersedes: none
evidence: measured
