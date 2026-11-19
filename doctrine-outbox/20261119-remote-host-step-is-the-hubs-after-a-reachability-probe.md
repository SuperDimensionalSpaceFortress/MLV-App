---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/remote-host-step-is-the-hubs-after-a-reachability-probe
rule: before a hub names any step on a remote venue host as the owner's, it runs one bounded read-only probe over the remote shell it already has (batch mode, a connect timeout, an outer deadline of at most 60 s, process-tree cleanup; it reads only identity, privilege and named paths, and changes no shell config or host-key trust) and emits a typed probe line. If the probe answers, the hub does the step itself, with the same verification a hand run would need. Reachability grants no authority: every owner gate and every never-authorized act still binds, and an act a guard denied is never retried through the remote shell, a file copy, another host or other command text. A remote write needs fresh evidence of no running measurement, no running job and no held lease, a recheck right before any stop, a pinned source sha verified on both ends, and exception-safe, verified recovery. It applies to any project whose hub drives a remote venue host.
mechanism: a GPU bench-host agent deploy was handed to the owner as two manual commands, and the host sat idle about 5.7 h while the hub host already held an elevated batch-mode shell to it. The premise "the hub cannot write the share" was true only of the file share; a file copy over the remote shell worked. The owner asked why they were in the loop. The fix ran in under a minute, and the next bench produced four counted legs.
check: the hub's scheduled-run template carries the directive (board-local; reviewed read-only before it went live)
supersedes: none
evidence: measured
