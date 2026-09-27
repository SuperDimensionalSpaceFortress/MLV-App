#!/usr/bin/env python3
"""LANE-NO-BACKGROUND-END-TURN-1: PreToolUse hook that denies backgrounded shell calls.

Wired per-lane by tools/coordination/Invoke-Lane.ps1, which writes a `--settings` file for
every Claude-engine lane registering a per-run COPY of this script against the `Bash|
PowerShell` matcher. A headless `claude -p` lane gets no later turn, so a call with
`run_in_background: true` promises a callback the process cannot receive -- the same shape
`--disallowedTools` already denies for Monitor/ScheduleWakeup/CronCreate/CronDelete/
RemoteTrigger/Workflow/TaskCreate. That mechanism cannot reach this case: `run_in_background`
is a parameter of a tool call, not a separate tool name, so denying it by name is not
possible. This hook closes exactly that gap, without touching NA-3 or adding any
CLAUDE_CODE_*/ANTHROPIC_*/OPENAI_* environment assignment (swarm ruling, 2026-09-22) -- see
docs/lane-containment.md.

Contract
--------
stdin  : one JSON object, ``{"tool_name": ..., "tool_input": {...}}`` (Claude Code's
         PreToolUse hook payload).
deny   : ``tool_input.run_in_background`` is truthy, for ANY ``tool_name`` -- not only
         ``Bash`` or ``PowerShell``. Round 7 (sol major 1 / fable major): the matcher
         registration narrows WHICH tool calls reach this script at all, but the script's
         own check no longer re-narrows by tool name on top of that -- a future tool
         carrying the same parameter is refused on the same basis without a second wiring
         change. Exits 2 with one line on stderr. This is the SAME fail-closed protocol
         tools/hooks/mlv-never-authorized.py uses (exit 2 = block, stderr is the reason),
         not the stdout-JSON permissionDecision protocol round 6 used -- round 7 unified on
         exit 2 so a launcher self-test can prove the gate with one exit-code check instead
         of parsing JSON, and so this script's own fail-closed default (below) needs no
         second signalling convention.
allow  : the flag absent or falsy -- exit 0, nothing on stdout or stderr.
fail-closed (round 7, sol major 2): missing, empty, or non-JSON stdin, or stdin whose
         top-level JSON value is not an object, or a present ``tool_input`` that is not an
         object, all DENY (exit 2) rather than allow. A hook that cannot read its own input
         has no basis to conclude the call is safe.

Pure. No side effects, no network, no filesystem access beyond stdin/stdout/stderr.
"""

import json
import sys

# Round 12 (fable minor), corrected round 14 (fable minor 2 -- this comment had itself drifted):
# the substring "headless lane" is load-bearing in ONE other file that must be hand-kept in sync
# with this literal -- tools/coordination/Invoke-Lane.ps1's $backgroundGateExpectedDenySubstring
# (the launcher's self-test requires this text in the captured output before it will trust the
# registered interpreter+argv pair denies at all). Round 12 named a second hand-duplicate site, a
# per-shell-candidate helper in this test file; round 13 deleted that helper along with the rest
# of the shell-candidate self-test machinery it classified, so only the one site above remains.
# tests/coordination/test_lane_containment.py's test_deny_reason_substring_matches_launcher_selftest
# is not a third hand-duplicate -- it reads both literals directly and also runs the real hook, so
# it fails if either literal drifts from the actual runtime deny text.
# Reword this string alone and the launcher's self-test fails the deny-reason check --
# fail-closed (every Claude lane launch is refused, loudly), but still an outage.
DENY_REASON = (
    "headless lane: run this command in the foreground with an explicit timeout; "
    "a headless lane has no later turn"
)


def should_deny(payload):
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return bool(tool_input.get("run_in_background"))


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else None
    except Exception as exc:
        sys.stderr.write("hook-error: malformed or non-JSON stdin: %s\n" % exc)
        return 2
    if payload is None:
        sys.stderr.write("hook-error: missing or empty stdin\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("hook-error: stdin JSON is not an object\n")
        return 2
    tool_input = payload.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, dict):
        sys.stderr.write("hook-error: tool_input is not an object\n")
        return 2
    if should_deny(payload):
        sys.stderr.write(DENY_REASON + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
