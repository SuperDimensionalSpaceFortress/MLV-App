---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin
rule: a test that mocks subprocess.run but not the native fast path before it depends on the host's process table (Windows ignores a pid's low two bits, so 789 can reach a live 788). Pin native_process_entry to None; never skip or deselect the test.
mechanism: test_server_wrapper_process_entry_prefers_pwsh_for_cim (test_server_wrapper_phase2.py:859) failed on the hosted runner with AttributeError on call_args=None, then passed on rerun. server.py:301 returns when win_process.py:314 native_process_entry answers, so the mock is never called. Forced onto a live pid and live pid+1: unpinned fails 2 of 2, pinned passes 2 of 2; unforced 40 of 40 pass. Upstream pinned it in cccf7b4b (PR 2), its trampoline sibling in cf4a9964 (PR 22); the in-tree copy has no pin. The hosted pid is a hypothesis.
applies: mlv-app
check: `py -3 -c "import ast,subprocess;s=subprocess.check_output(['git','show','fork/master:tools/agent-bridge/test_server_wrapper_phase2.py'],text=True);c=[n for n in ast.parse(s).body if getattr(n,'name','')=='ServerWrapperPhase2Tests'][0];m=[n for n in c.body if getattr(n,'name','')=='test_server_wrapper_process_entry_prefers_pwsh_for_cim'][0];w=[i.context_expr for n in ast.walk(m) if isinstance(n,ast.With) for i in n.items];print('PINNED' if not m.decorator_list and any(isinstance(x,ast.Call) and ast.unparse(x.func)=='mock.patch.object' and [getattr(a,'id',getattr(a,'value',0)) for a in x.args[:2]]==['_server','native_process_entry'] and any(k.arg=='return_value' and getattr(k.value,'value',0) is None for k in x.keywords) for x in w) else 'UNPINNED')"` prints UNPINNED now; fixed only when PINNED. Any error or nonzero exit (git 128, retired file) is UNKNOWN. Guard: test_server_wrapper_phase2.py::ServerWrapperPhase2Tests::test_server_wrapper_process_entry_prefers_pwsh_for_cim in layibabalola/agent-bridge.
supersedes: none
evidence: measured
