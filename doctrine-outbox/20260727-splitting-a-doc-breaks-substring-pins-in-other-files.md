---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-07-27 — splitting/moving a doc silently breaks exact-substring pins scattered across other files, and one green checker does not mean the contract holds

**Symptom (measured):** a repo had over a hundred exact-substring assertions spread across roughly eight documentation files, each pin binding a piece of prose to a specific file path (enforced by two independent surfaces: a structured `{path, contains}` baseline checker and a plain `assertIn`-style test suite). After the first attempt to split one large doc into smaller files, the structured baseline checker reported fully green (`ok=True`) — but a separate test in the suite, which pinned a dozen of the same tokens via plain `assertIn` calls (including a loop form checking several text blobs at once), went red. The move had silently dropped tokens that only the second, less-obvious enforcement surface was checking.

**Cause:** when multiple independent mechanisms enforce "this exact text must remain at this path," a content *move* (not just a deletion or rewrite) breaks every pin as effectively as an outright removal, because the pin binds to a location, not just to the string's existence somewhere in the repo. Each enforcement surface only catches the pins it itself was written to check; passing one surface says nothing about the others. A single "the gate is green" signal is a property of one gate, not of the contract.

**Rule:** before splitting, shrinking, or relocating any doc that other files or tests reference by exact substring, first enumerate **every** pinning surface (grep all test/check files for the doc's distinctive phrases, not just the one official checker), and verify the split against all of them — not just the primary automated gate. Treat "the structured checker passed" as necessary, not sufficient, evidence that a doc move preserved its contract.

**Falsifier:** after any doc split, run the full test suite (not just the doc-content checker) and specifically look for `assertIn`/exact-string test failures on moved content; a green structured checker alongside a red test suite is exactly this trap.
