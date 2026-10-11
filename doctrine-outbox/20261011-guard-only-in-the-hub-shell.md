---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/guard-only-in-the-hub-shell
rule: a never-authorized rule is enforced on every surface that can act on it (the hub shell, venue job scripts, lane wrappers and CI), and each surface carries its own RED fixture; a guard on one surface is not evidence for the others.
mechanism: the rule was checked only in the hub shell, so the same action stayed possible from any other process that could reach the target, and the existing guard gave a false sense of coverage. A rule that can be dodged by changing the launching surface is a convention on those other surfaces.
check: list the surfaces that can perform the action; each row names its guard and its RED fixture, and a surface with no fixture is a finding. The count of surfaces without a fixture is printed with the rule.
supersedes: none
evidence: reported
