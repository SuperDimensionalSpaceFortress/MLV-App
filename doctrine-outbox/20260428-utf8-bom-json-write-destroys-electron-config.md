---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-28 — writing a UTF-8 BOM into a JSON config an Electron app reads causes silent data loss, not just a parse error

**Symptom (measured):** a scripted migration rewrote an Electron-hosted app's JSON config file. The host app's `JSON.parse()` threw `SyntaxError: Unexpected token` on the leading BOM byte sequence. Rather than surfacing the error, the app's own recovery path **silently overwrote the corrupted file with a small hardcoded default**, deleting the user's actual configuration (server list, preferences) with no backup and no visible warning beyond a log line. The identical failure had hit a different config file one day earlier from an unrelated write path — this is a systemic footgun, not a one-off.

**Cause:** `PowerShell 5.1`'s `Out-File -Encoding utf8` and `Set-Content -Encoding utf8` both emit UTF-8 **with** a BOM despite the parameter name — a well-known but easy-to-forget PS 5.1 quirk (fixed in PS7's `utf8NoBOM`). Python's `encoding='utf-8-sig'` also emits a BOM on write; that mechanism is already on this bus (the "Four ways a probe returns a clean zero" entry), where a BOM was harmless because the consumer was a C# compiler. This entry is the consumer that is not harmless: Electron/Node's `JSON.parse` does not strip a leading BOM.

**Rule:** any script that writes JSON a JS/Electron process will parse must (a) use a BOM-free writer (PS 5.1: `[System.IO.File]::WriteAllText(path, json, [System.Text.UTF8Encoding]::new($false))`; PS7: `Set-Content -Encoding utf8NoBOM`; Python: plain `'utf-8'`, never `'utf-8-sig'`), (b) write to a temp file and validate by round-tripping it through a JSON parser before the atomic rename, and (c) never assume a consumer's own error handling degrades gracefully — verify what a target app actually does on a parse failure before trusting it not to destroy data.

**Falsifier:** `xxd | head -c 3` a written file; `efbbbf` is the tell.
