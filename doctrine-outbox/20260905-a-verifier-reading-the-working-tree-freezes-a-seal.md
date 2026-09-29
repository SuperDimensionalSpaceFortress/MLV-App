---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-05 — a verifier that re-reads the working tree to check a sealed/attested receipt turns every seal into a permanent freeze on the files it names

**Symptom (measured):** two verification paths over the same repository disagreed about whether a sealed receipt was valid — one reported PASS, the other reported FAILED — despite both running against the identical git tree at the identical moment. This disagreement was misread as evidence that the receipt needed to be re-sealed, and blocked a pull request for roughly two weeks on a "when should we re-seal" question that turned out not to exist.

**Cause:** one verification component read the file it was checking straight from the current **working tree**; the sibling component that produced/validated the seal read the same logical file from the commit that was attested **at seal time**. As soon as the file changed on disk after the seal (an unrelated, legitimate edit), the working-tree reader started evaluating different bytes than the seal had committed to — while still nominally checking "the same file." A verifier built this way doesn't strengthen the attestation; it silently converts the seal into a freeze order on every file it touches, because any future edit to those files makes the seal appear to fail, forever, with no re-seal actually required.

**Rule:** a component that checks a sealed/attested artifact must always fetch the attested bytes **from the attested commit** (`git show <sealed-sha>:<path>`), never from the current working tree or a default branch tip — regardless of how convenient the working-tree path is to wire up. The anchor must itself be pinned: the first version of the fix passed when the recorded head was repointed at another commit whose copies of the bound files happened to match, and was closed by also asserting the head's tree hash equals the tree the receipt recorded.

**Falsifier:** edit a file that is part of a previously-sealed receipt, without touching the sealed commit, and re-run both verification paths; a verifier that flips to FAILED on that unrelated edit is reading the working tree, not the attested commit, and needs the fix above.
