---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/pin-runner-cache-never-repairs-same-size-corruption
rule: a blob cache that is re-verified on read must also be repaired on write: when the bus clone supplies verified bytes, `cache_put` replaces a cached file that is present but not the pinned bytes, whatever its size.
mechanism: bus tools/factory/pin-runner.py (pin 28fc1ee1; vendored at MLV-App factory-pins/pin-runner.py:312-331): `cache_get` ignores a cached blob that fails its hash, `pinned_bytes` then reads the bus and calls `cache_put`, which returns early when the cached file has the same size as the new bytes. A same-size corrupt entry (one flipped byte) survives every bus-backed run. Measured 2026-10-18: a run with a bus clone exits 0 and leaves the entry corrupt; the next run with no clone exits 3 UNAVAILABLE although the pin had run cleanly on that box. Never wrong bytes (the failure is loud), but the docstring promise that a seen pin keeps running without the clone does not hold.
check: `py -3 -m pytest tests/coordination/test_check_doc_size_by_pin.py -k same_size_corrupt -q` asserts the safe half (corrupt bytes never run). The repair is the bus owner's: in `cache_put`, skip only when the cached file passes `check_bytes`; then re-pin.
supersedes: none
evidence: measured
- **MLV meanwhile:** it edits no pinned bus artifact. A corrupt entry costs one offline failure (exit 3, nothing runs); cure: delete `%LOCALAPPDATA%/factory-pins/blobs/<blob id>`, run once with the clone present (docs/22 "Pinned runs").
- **Origin:** CoS #491 I1, folded into CHECK-DOC-SIZE-SINGLE-SOURCE-1.
