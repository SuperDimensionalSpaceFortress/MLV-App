"""Report lanes that ran an older model than their family's newest, or any effort but 'high'.

MODEL-POSTURE-R16-1 (doctrine R16: always the latest model of each family, all at high effort).
Invoke-Lane.ps1 records, per run, the lane, the model id that actually ran (`resolvedModel`),
the effort and `startedUtc` in <runs-dir>/<run>/<lane>-001.receipt.json. This script reads those
receipts and answers two questions per lane, inside a recency window (--since-hours, default 72;
0 = every receipt):

  * STALE-MODEL  the lane's two most recent runs BOTH used an id older than the newest id seen
                 for that model FAMILY -- "twice in a row", so one run that was already in flight
                 when a new model shipped does not raise it. Families compare by family, never by
                 lane: 'claude-opus' / 'claude-sonnet' / 'claude-fable' for the claude CLI, the
                 tier ('sol', 'luna', 'astra') for a codex slug 'gpt-<version>-<tier>'. Versions
                 compare numerically ('6.1' beats '6'; 'claude-opus-5-5' beats 'claude-opus-5').
  * BAD-EFFORT   any receipt in the window whose recorded effort is anything but 'high'. A receipt
                 that recorded no effort (an old schema) is listed under `unrecordedEffort`, never
                 silently counted as high and never flagged as a violation.

The newest id per family is the highest version among the receipts read, raised by any
--newest FAMILY=ID the caller supplies (the way to say "the CLI now offers claude-opus-5-6" before
any lane has run it). Only receipts that proved a model (a parseable resolvedModel; 'unknown' and
null never count) take part in the model rule.

Missing evidence is never CURRENT (r3 blocker B1). A lane's verdict is, in order:

  STALE       a STALE-MODEL or BAD-EFFORT reason above.
  NO-DATA     the lane has no proven model, or its NEWEST run in the window has none (a failed or
              still-running run, resolvedModel null/'unknown'): what the lane runs now is unproven.
  INCOMPLETE  the model is proven but at least one run in the window recorded no effort, so
              'high' is unproven; counted in `unrecordedEffort` and shown in the table.
  CURRENT     a proven model, every effort recorded and 'high'.

The overall verdict is STALE if any lane is, else INCOMPLETE if any lane is NO-DATA or INCOMPLETE,
else NO-DATA when no lane ran in the window, else CURRENT.

Exit code: 0 overall CURRENT, 1 at least one STALE-MODEL or BAD-EFFORT, 2 the runs directory is
unreadable, 3 overall INCOMPLETE or NO-DATA (evidence missing, not a violation). One JSON object with
--json, else a short table.

Usage: python check-lane-model-currency.py [--runs-dir DIR] [--since-hours N] [--newest FAMILY=ID]... [--json]
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

CLAUDE_ID_RE = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<version>[0-9]+(?:-[0-9]+)*?)(?:-[0-9]{8})?$")
CODEX_ID_RE = re.compile(r"^gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<family>[a-z0-9]+)$")
REQUIRED_EFFORT = "high"
CONSECUTIVE_STALE_RUNS = 2
DEFAULT_SINCE_HOURS = 72.0
RECEIPT_GLOB = "*/*-001.receipt.json"


def parse_model_id(model_id):
    """'claude-opus-5-5' -> ('claude-opus', (5, 5)); 'gpt-6.1-sol' -> ('sol', (6, 1)); else None."""
    if not isinstance(model_id, str):
        return None
    text = model_id.strip()
    match = CLAUDE_ID_RE.match(text)
    if match:
        return "claude-" + match.group("family"), tuple(int(p) for p in match.group("version").split("-"))
    match = CODEX_ID_RE.match(text)
    if match:
        try:
            return match.group("family"), tuple(int(p) for p in match.group("version").split("."))
        except ValueError:
            return None
    return None


def parse_utc(text):
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    cleaned = re.sub(r"(\.[0-9]{6})[0-9]+", r"\1", cleaned)
    try:
        moment = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def default_runs_dir():
    """<repo>/.claude-state/fleet-runs of the MAIN checkout: .claude-state is untracked, so a
    linked worktree has none; git's common dir leads back to the checkout that does."""
    override = os.environ.get("MLV_FLEET_RUNS_DIR")
    if override:
        return override
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True,
                                timeout=15, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    root = os.path.dirname(os.path.abspath(common)) if common else os.getcwd()
    return os.path.join(root, ".claude-state", "fleet-runs")


def load_receipts(runs_dir):
    """-> list of normalized dicts, oldest first. Unreadable receipts are counted, not fatal."""
    receipts, unreadable = [], 0
    for path in glob.glob(os.path.join(runs_dir, RECEIPT_GLOB)):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("lane"), str):
            unreadable += 1
            continue
        started = parse_utc(data.get("startedUtc"))
        if started is None:
            unreadable += 1
            continue
        resolved = data.get("resolvedModel")
        receipts.append({
            "path": path, "lane": data["lane"], "started": started, "state": data.get("state"),
            "card": data.get("card"), "effort": data.get("effort"),
            "resolvedModel": resolved if isinstance(resolved, str) else None,
            "parsed": parse_model_id(resolved),
        })
    receipts.sort(key=lambda r: r["started"])
    return receipts, unreadable


def newest_per_family(receipts, overrides):
    newest = {}
    for receipt in receipts:
        if receipt["parsed"]:
            family, version = receipt["parsed"]
            if family not in newest or version > newest[family][0]:
                newest[family] = (version, receipt["resolvedModel"])
    for family, model_id in overrides.items():
        parsed = parse_model_id(model_id)
        if parsed and parsed[0] == family and (family not in newest or parsed[1] > newest[family][0]):
            newest[family] = (parsed[1], model_id)
    return newest


def evaluate(receipts, since_hours=DEFAULT_SINCE_HOURS, now=None, overrides=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    # The family's newest id is judged over every receipt, the window only decides what is reported.
    newest = newest_per_family(receipts, overrides or {})
    if since_hours and since_hours > 0:
        cutoff = now - datetime.timedelta(hours=since_hours)
        window = [r for r in receipts if r["started"] >= cutoff]
    else:
        window = list(receipts)
    lanes = {}
    for receipt in window:
        lanes.setdefault(receipt["lane"], []).append(receipt)
    report = {}
    for lane in sorted(lanes):
        runs = lanes[lane]
        proven = [r for r in runs if r["parsed"]]
        reasons = []
        # STALE-MODEL: the two most recent proven runs are both older than their family's newest.
        tail = proven[-CONSECUTIVE_STALE_RUNS:]
        stale_tail = len(tail) == CONSECUTIVE_STALE_RUNS and all(
            r["parsed"][1] < newest[r["parsed"][0]][0] for r in tail)
        if stale_tail:
            family = tail[-1]["parsed"][0]
            reasons.append({
                "kind": "STALE-MODEL", "family": family, "newest": newest[family][1],
                "ranModels": [r["resolvedModel"] for r in tail], "receipts": [r["path"] for r in tail]})
        bad = [r for r in runs if isinstance(r["effort"], str) and r["effort"].strip() and r["effort"] != REQUIRED_EFFORT]
        if bad:
            reasons.append({
                "kind": "BAD-EFFORT", "efforts": sorted({r["effort"] for r in bad}),
                "receipts": [r["path"] for r in bad[-5:]], "count": len(bad)})
        unrecorded = [r for r in runs if not (isinstance(r["effort"], str) and r["effort"].strip())]
        latest = proven[-1] if proven else None
        if reasons:
            verdict = "STALE"
        elif not proven or not runs[-1]["parsed"]:
            verdict = "NO-DATA"
        elif unrecorded:
            verdict = "INCOMPLETE"
        else:
            verdict = "CURRENT"
        report[lane] = {
            "verdict": verdict,
            "runsInWindow": len(runs), "provenModelRuns": len(proven),
            "latestModel": latest["resolvedModel"] if latest else None,
            "familyNewest": newest[latest["parsed"][0]][1] if latest else None,
            "reasons": reasons, "unrecordedEffort": len(unrecorded),
        }
    verdicts = {entry["verdict"] for entry in report.values()}
    if "STALE" in verdicts:
        overall = "STALE"
    elif verdicts - {"CURRENT"}:
        overall = "INCOMPLETE"
    else:
        overall = "CURRENT" if report else "NO-DATA"
    return {
        "sinceHours": since_hours, "receiptsRead": len(receipts), "receiptsInWindow": len(window),
        "familyNewest": {family: pair[1] for family, pair in sorted(newest.items())},
        "lanes": report, "verdict": overall,
    }


def parse_overrides(items):
    overrides = {}
    for item in items or []:
        family, sep, model_id = item.partition("=")
        parsed = parse_model_id(model_id)
        if not sep or not parsed or parsed[0] != family:
            raise ValueError("--newest %r must be FAMILY=ID with the id belonging to that family "
                             "(e.g. claude-opus=claude-opus-5-6, sol=gpt-6.2-sol)" % item)
        overrides[family] = model_id
    return overrides


def render(result):
    lines = ["lane model currency (window %sh, %d of %d receipts) -> %s" % (
        result["sinceHours"], result["receiptsInWindow"], result["receiptsRead"], result["verdict"])]
    for lane, entry in result["lanes"].items():
        lines.append("  %-7s %-10s latest=%s family-newest=%s runs=%d%s" % (
            lane, entry["verdict"], entry["latestModel"], entry["familyNewest"], entry["runsInWindow"],
            " unrecordedEffort=%d" % entry["unrecordedEffort"] if entry["unrecordedEffort"] else ""))
        for reason in entry["reasons"]:
            if reason["kind"] == "STALE-MODEL":
                lines.append("          STALE-MODEL: ran %s, newest %s %s" % (
                    ", ".join(reason["ranModels"]), reason["family"], reason["newest"]))
            else:
                lines.append("          BAD-EFFORT: %s x%d" % ("/".join(reason["efforts"]), reason["count"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs-dir", default=None)
    parser.add_argument("--since-hours", type=float, default=DEFAULT_SINCE_HOURS)
    parser.add_argument("--newest", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runs_dir = args.runs_dir or default_runs_dir()
    if not os.path.isdir(runs_dir):
        print(json.dumps({"ok": False, "error": "runs-dir-not-found:%s" % runs_dir}))
        return 2
    try:
        overrides = parse_overrides(args.newest)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": "bad-newest:%s" % exc}))
        return 2
    receipts, unreadable = load_receipts(runs_dir)
    result = evaluate(receipts, args.since_hours, overrides=overrides)
    result["ok"] = True
    result["runsDir"] = runs_dir
    result["unreadableReceipts"] = unreadable
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return {"CURRENT": 0, "STALE": 1}.get(result["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
