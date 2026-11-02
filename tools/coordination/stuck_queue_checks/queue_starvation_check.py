"""Executable check for the doctrine card queue-without-age-limits-starves-owner-waiting-card.

Fixture: 50 due P1/P2 rows (1 h old, inside their limits) and one due P3 row that is 25 h
old with ownerWaiting set. One drain beat of capacity 3 runs against it. The check passes
only when the P3 row is reported starved (25 h is over the 24 h P3 limit), is bumped one
priority (P3 -> P2), and is drained although 50 higher-priority rows were ahead of it.

The drain under test is a command: it reads {"capacity": N, "rows": [...]} on stdin and
prints {"drained": [ids], "starved": [ids], "bumped": {id: new_priority}} on stdout.
Two reference drains ship with the check so it is RED and GREEN on its own:

  py -3 queue_starvation_check.py --impl naive   -> RED   (exit 1): priority then FIFO
  py -3 queue_starvation_check.py --impl aged    -> GREEN (exit 0): age limits + owner-waiting first
  py -3 queue_starvation_check.py --drain-cmd "<a board's own drain command>"

Exit: 0 GREEN, 1 RED (the trap is present), 2 harness or usage error.
"""

import argparse
import json
import subprocess
import sys

CAPACITY = 3
OWNER_ID = "OWNER-P3"
# Starvation limits in hours by priority (the STUCK-RECONCILER-1 values named on the card).
LIMIT_HOURS = {1: 2, 2: 6, 3: 24, 4: 72, 5: 72}


def fixture():
    rows = [
        {"id": f"P{1 + i % 2}-{i:02d}", "priority": 1 + i % 2, "ageHours": 1.0, "ownerWaiting": False}
        for i in range(50)
    ]
    rows.append({"id": OWNER_ID, "priority": 3, "ageHours": 25.0, "ownerWaiting": True})
    return {"capacity": CAPACITY, "rows": rows}


def drain_naive(request):
    ordered = sorted(request["rows"], key=lambda r: r["priority"])  # stable: FIFO inside a priority
    return {"drained": [r["id"] for r in ordered[: request["capacity"]]], "starved": [], "bumped": {}}


def drain_aged(request):
    rows = request["rows"]
    starved = {r["id"] for r in rows if r["ageHours"] > LIMIT_HOURS[r["priority"]]}
    bumped = {r["id"]: max(1, r["priority"] - 1) for r in rows if r["id"] in starved}

    def key(r):
        return (not r["ownerWaiting"], r["id"] not in starved, bumped.get(r["id"], r["priority"]), -r["ageHours"])

    ordered = sorted(rows, key=key)
    return {"drained": [r["id"] for r in ordered[: request["capacity"]]], "starved": sorted(starved), "bumped": bumped}


def run_command(cmd, request):
    done = subprocess.run(cmd, shell=True, input=json.dumps(request), capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"drain command exited {done.returncode}: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)


def verdict(result):
    """Return the list of failed expectations; empty means GREEN."""
    failed = []
    if OWNER_ID not in result.get("starved", []):
        failed.append(f"{OWNER_ID} aged 25 h (limit 24 h) was not reported starved")
    if result.get("bumped", {}).get(OWNER_ID) != 2:
        failed.append(f"{OWNER_ID} was not bumped P3 -> P2 (bumped={result.get('bumped')})")
    if OWNER_ID not in result.get("drained", []):
        failed.append(f"{OWNER_ID} was not drained this beat (drained={result.get('drained')})")
    return failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--impl", choices=("naive", "aged"))
    group.add_argument("--drain-cmd")
    args = parser.parse_args(argv)
    request = fixture()
    try:
        if args.drain_cmd:
            result = run_command(args.drain_cmd, request)
        else:
            result = {"naive": drain_naive, "aged": drain_aged}[args.impl](request)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR harness could not run the drain: {exc}")
        return 2
    failed = verdict(result)
    label = args.impl or "drain-cmd"
    if failed:
        print(f"RED {label}: the trap is present")
        for line in failed:
            print(f"  - {line}")
        return 1
    print(f"GREEN {label}: {OWNER_ID} starved, bumped to P2 and drained ahead of 50 P1/P2 rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
