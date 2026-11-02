---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin
rule: a test that mocks subprocess.run but not the native fast path before it depends on the host's process table; Windows opens a pid with its low two bits ignored, so a made-up pid 789 can reach a live pid 788. Pin native_process_entry to None, assert run.called before reading call_args, never skip or deselect the test.
mechanism: test_server_wrapper_process_entry_prefers_pwsh_for_cim (tools/agent-bridge/test_server_wrapper_phase2.py:859) failed on the hosted runner at :872, AttributeError 'NoneType' has no attribute 'args' (a fresh 14-test unittest step, 2.4 s), then passed on rerun. server.py:295 _wrapper_process_entry returns at :301 when core/win_process.py:314 native_process_entry answers, so the mock is never called. Forcing the test body onto a live pid and onto live pid+1 fails it unpinned 2 of 2 and passes it pinned 2 of 2; unforced 40 of 40 pass. Upstream pinned it in cccf7b4b (PR 2) and its trampoline sibling in cf4a9964 (PR 22); the in-tree copy has no pin. The hosted pid is a hypothesis; the log prints no pids.
applies: mlv-app
check: `py -3 -c "import ast,subprocess;s=subprocess.check_output(['git','show','fork/master:tools/agent-bridge/test_server_wrapper_phase2.py'],text=True);f=[n for n in ast.walk(ast.parse(s)) if getattr(n,'name','')=='test_server_wrapper_process_entry_prefers_pwsh_for_cim'][0];print('PINNED' if 'native_process_entry\x22, return_value=None' in ast.get_source_segment(s,f) else 'UNPINNED')"` prints UNPINNED now; fixed only when it prints PINNED. Any error or nonzero exit (dubious ownership 128, a retired file) is UNKNOWN, never fixed. Guard: test_server_wrapper_phase2.py::ServerWrapperPhase2Tests::test_server_wrapper_process_entry_prefers_pwsh_for_cim in layibabalola/agent-bridge.
supersedes: none
evidence: measured
