---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/scanner-narrowing-needs-a-generated-must-refuse-sweep
rule: a round that NARROWS what a refusal scanner reads (it reads a slot or an operand in fewer places, to cure a false alarm) commits, in the same commit, a generated must-refuse sweep over every parameter spelling that can stand in front of the narrowed slot: each name, alias and unambiguous prefix taken from the cmdlet's own metadata (`(Get-Command <cmdlet>).Parameters`, common parameters included), plus an unknown and an ambiguous spelling, each put before the slot in a real refused case. A spelling that binds to no parameter or to several is read as a switch (fail closed). Never build the spelling list by hand: proof that the false alarms are gone is not proof that nothing new gets through.
mechanism: 2026-11-14, a WMI process-create scanner narrowed "a class is read only where it is named" (New-Object's type is its first positional) and took switch-ness from a hand list. `-vb` and `-db` are aliases of Verbose and Debug, not prefixes, so `New-Object -vb System.Management.ManagementClass ...` had its type read as the alias's value and a direct process create passed; the round before refused it. A cross-provider review reproduced it. The generated sweep then found 40 surviving spellings (`-v`, `-s`, an unknown name, ambiguous prefixes) and, on a sibling slot, an ambiguous `-in` read as -InputObject.
applies: mlv-app
check: in mlv-app, pytest tools/coordination/test_coordination_guardrails.py -k "every_new_object_parameter_spelling or every_foreach_object_parameter_spelling"
supersedes: none
evidence: measured
