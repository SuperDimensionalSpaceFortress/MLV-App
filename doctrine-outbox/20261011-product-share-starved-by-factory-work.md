---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/product-share-starved-by-factory-work
rule: a receipt: a board that wants product progress reserves a producer slot for a declared product card and sets a share floor on merged product work; factory cards take a slot only when no product card is due or they name the product subject they unblock. MLV is trialling this remedy.
mechanism: in 72 hours 9 of 68 first-parent merges (13 percent) touched product paths, while the hub counted more factory rows than product rows in its queue; with no reserved slot the loop kept choosing work on itself.
check: git log refs/remotes/fork/master --first-parent --merges --since="72 hours ago" counts the merges; classify each by whether it touches product source paths and report the share against the floor.
supersedes: none
evidence: measured
