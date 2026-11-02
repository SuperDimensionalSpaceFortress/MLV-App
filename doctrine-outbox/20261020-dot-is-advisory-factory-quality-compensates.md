---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/dot-is-advisory-factory-quality-compensates
rule: In mlv-app, CoS (Dot) review is advisory, never a merge precondition: the fleet CoS merge-gate is DISTINGUISHED here. Merge bar: two authenticated exact-head keys, green hosted CI, CLEAN, not draft, plus a third adversarial key on HIGH-RISK paths. The 24 h ACK/DISTINGUISH reply duty stays.
mechanism: Owner ruling 2026-10-20, verbatim: "I dont want dot to be a blocker. Dot's review is a nice to have but the software factory should be shipping good code. Id rather improve factory's quality than let dot be a blocker. Lets update the posture so we keep throughput and quality somehow". Compensation: (1) key3, primed with the key attack catalogue and the Dot record as data, on HIGH-RISK paths; (2) Dot findings naming a concrete defect become a fix round (open PR) or a P1 fix card plus revert card (merged PR); (3) 7-day escape metric, Dot-demonstrated defects on merged PRs plus reverts: above 2 widens the catalogue and the key3 path list.
check: git grep -n -i "never merge a card" -- CLAUDE.md AGENTS.md returns nothing
supersedes: none
evidence: reported
