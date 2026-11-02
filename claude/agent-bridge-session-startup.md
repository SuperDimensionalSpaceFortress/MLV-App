# Agent Bridge - Session Startup (detail)

Demoted verbatim from [`CLAUDE.md`](../CLAUDE.md) under [docs/22-doc-fragmentation-policy.md](../docs/22-doc-fragmentation-policy.md). Headings are unchanged and remain the stable IDs referenced from the parent index.

> **Status 2026-10-17: dormant.** No settings file wires `bootstrap_session.py` as a SessionStart hook any more, and the September 9 owner ruling suspends in-tree `tools/agent-bridge/` work ([agents/agent-bridge-source-of-truth.md](../agents/agent-bridge-source-of-truth.md)). Follow this only when a session is explicitly bridged to a Codex peer.

## Agent Bridge — Session Startup (Hook-Driven)

This repo uses an agent-bridge to coordinate with a peer Codex session. The
`SessionStart` hook in `.claude/settings.local.json` runs
`tools/agent-bridge/bootstrap_session.py` automatically at the start of every
session — its stdout is injected into your context. The bootstrap registers
this session as the active Claude bridge owner (superseding any older Claude
session), drains messages from the previous session, sends a HANDSHAKE to
Codex, and updates the watcher config.

After the hook fires, do these in order:
1. **Read `drained_previous_messages`** in the hook output — surface any unread
   messages from the previous session to the user before proceeding.
2. **Use the returned `session_id`** as your active Claude bridge GUID for this session.
3. **If `check_inbox` returns a `SESSION_UPDATE: superseded` control message at any
   point**, stop all bridge sends immediately — a newer Claude session has taken over.
4. **Read `active_session_unread`** in the hook output — these are unread rows
   already sitting in the new active session bucket. Surface them, then mark each
   read by id after handling.
5. **Start the bridge Monitor** — the Monitor is Claude's inbox wake mechanism and does
   NOT survive context compaction. Start it every session, no exceptions:
   ```
   Monitor(persistent=True, command="<python> -u tools/agent-bridge/bridge_monitor_poll.py --state-dir <bridge-state-dir> --agent claude --session-id <active-guid> --project mlv-app --poll-interval-seconds 2")
   ```
   Use `bridge_monitor_poll.py` for the Monitor. Do not substitute
   `probe_server.py`; probes are diagnostics and will not keep Claude's inbox
   wake path armed. Before saying "waiting for Codex," verify the Monitor task
   is active. If no Monitor is running, start one before waiting.
   If a bridge message arrives with `TYPE: CONTROL` and
   `SUBJECT: MONITOR_RESTART_REQUIRED`, stop any stale Monitor task handle and
   immediately start a fresh `bridge_monitor_poll.py` Monitor with the command
   shown in the message. The watcher sends this control when it detects a stale
   or missing Monitor heartbeat, or when bridge Monitor-related code changes.

When a Monitor notification fires, call `mcp__agent-bridge__check_inbox` with
`agent=claude`, `session_id=<active-guid-or-mlv-app>`, `mark_read=False`, then mark
each message read explicitly by id.

If the hook output is missing from your session-start context (broken JSON,
deleted file, hook failure), fall back to running `bootstrap_session.py`
manually with the command stored in `.claude/settings.local.json`.

Bridge protocol details: `tools/agent-bridge/BRIDGE_PROTOCOL.md`
Hardening plan and audit log: `tools/agent-bridge/BRIDGE_HARDENING.md`
