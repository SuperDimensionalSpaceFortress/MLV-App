#!/usr/bin/env python3
"""Compare the Look Assist analysis of two smoke-run.log files, field by field.

LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: the analysis moved its heavy calls off the GUI thread and has to land on the
same look. A control arm and a treatment arm of one clip and one backend must report the same analysis.

Compared, in log order (a daylight fallback re-runs the apply, so an event can appear more than once):
    look_assist.apply.result, look_assist.apply.auto_wb, look_assist.apply.color_balance,
    look_assist.apply.post_balance, look_assist.window_lit_interior, look_assist.display_meter,
    and the LOOK_STATE_FIELDS of gui_smoke.visual_state.

Excluded, and why:
    - the timestamp and thread id: they are the line's prefix, not a field, and differ on every run.
    - last_serial, next_serial (apply.result): render-request counters. Waiting for a running render before the
      analysis moves them, and neither is an input of the look.
    - every other visual_state field: GPU route, scope and window state, not the look.

Exit 0 identical, 1 differs or a requirement failed, 2 usage or an unreadable log. A check that finds nothing to
compare fails: an arm with no analysis, or with Look Assist off, can never read as identical.
"""
import argparse
import json
import re
import sys
from datetime import datetime

COMPARED_EVENTS = (
    "look_assist.apply.result",
    "look_assist.apply.auto_wb",
    "look_assist.apply.color_balance",
    "look_assist.apply.post_balance",
    "look_assist.window_lit_interior",
    "look_assist.display_meter",
)
VISUAL_STATE_EVENT = "gui_smoke.visual_state"
LOOK_STATE_FIELDS = (
    "look_assist_enabled",
    "look_assist_diagnostics_valid",
    "look_assist_scene",
    "exposure",
    "contrast",
    "pivot",
    "shadows",
    "highlights",
    "vibrance",
    "temperature",
    "tint",
    "raw_black",
    "raw_white",
    "chroma_smooth",
    "look_assist_flavor",
)
EXCLUDED_FIELDS = frozenset(("last_serial", "next_serial"))
REQUIRED_RESULT_FIELDS = ("analysis", "scene", "final_temp", "final_tint")

_LINE_RX = re.compile(
    r"^\[(?P<stamp>[^\]]+)\]\s+\[[^\]]+\]\s+\[[^\]]+\]\s+interaction_trace\s+event=(?P<event>\S+)\s*(?P<body>.*)$"
)
_FIELD_RX = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S*)")


def parse_log(text):
    """Returns [(line number, stamp, event, {field: value})] for every interaction_trace line."""
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _LINE_RX.match(line)
        if not match:
            continue
        fields = {m.group("key"): m.group("value") for m in _FIELD_RX.finditer(match.group("body"))}
        rows.append((number, match.group("stamp"), match.group("event"), fields))
    return rows


def _compared_rows(rows):
    out = {}
    for _number, _stamp, event, fields in rows:
        if event in COMPARED_EVENTS:
            out.setdefault(event, []).append({k: v for k, v in fields.items() if k not in EXCLUDED_FIELDS})
        elif event == VISUAL_STATE_EVENT:
            out.setdefault(event, []).append({k: fields.get(k) for k in LOOK_STATE_FIELDS})
    return out


def _event_order(rows):
    return [event for _number, _stamp, event, _fields in rows if event in COMPARED_EVENTS or event == VISUAL_STATE_EVENT]


def compare_rows(control_rows, treatment_rows):
    """Returns the list of differences, one string each."""
    control = _compared_rows(control_rows)
    treatment = _compared_rows(treatment_rows)
    differences = []
    control_order = _event_order(control_rows)
    treatment_order = _event_order(treatment_rows)
    if control_order != treatment_order:
        differences.append("event order: control=%s treatment=%s" % (",".join(control_order), ",".join(treatment_order)))
    for event in COMPARED_EVENTS + (VISUAL_STATE_EVENT,):
        left = control.get(event, [])
        right = treatment.get(event, [])
        if len(left) != len(right):
            differences.append("%s: %d line(s) in control, %d in treatment" % (event, len(left), len(right)))
        for index, (a, b) in enumerate(zip(left, right)):
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    differences.append("%s[%d].%s: control=%s treatment=%s" % (event, index, key, a.get(key), b.get(key)))
    return differences


def _last_result(rows):
    results = [fields for _n, _s, event, fields in rows if event == "look_assist.apply.result"]
    return results[-1] if results else None


def requirement_failures(rows, arm, require_walk=False, require_surface_search=False):
    """What one arm must show for the comparison to mean anything."""
    failures = []
    result = _last_result(rows)
    if result is None:
        failures.append("%s: no look_assist.apply.result line" % arm)
    for fields in (fields for _n, _s, event, fields in rows if event == "look_assist.apply.result"):
        for key in REQUIRED_RESULT_FIELDS:
            if not fields.get(key):
                failures.append("%s: look_assist.apply.result has no %s" % (arm, key))
    states = [fields for _n, _s, event, fields in rows if event == VISUAL_STATE_EVENT]
    if not states:
        failures.append("%s: no %s line" % (arm, VISUAL_STATE_EVENT))
    for fields in states:
        if fields.get("look_assist_enabled") != "1":
            failures.append("%s: look_assist_enabled=%s (Look Assist must be on in every leg)" % (arm, fields.get("look_assist_enabled")))
        if fields.get("look_assist_diagnostics_valid") != "1":
            failures.append("%s: look_assist_diagnostics_valid=%s, the analysis did not land" % (arm, fields.get("look_assist_diagnostics_valid")))
        for key in LOOK_STATE_FIELDS:
            if not fields.get(key):
                failures.append("%s: %s has no %s" % (arm, VISUAL_STATE_EVENT, key))
    if result is not None and require_walk and result.get("post_walk_ran") != "1":
        failures.append("%s: post_walk_ran=%s, this arm cannot catch a skipped refinement" % (arm, result.get("post_walk_ran")))
    if result is not None and require_surface_search and result.get("surface_search") != "converged":
        failures.append("%s: surface_search=%s, this arm does not cover the window-lit route" % (arm, result.get("surface_search")))
    return failures


def play_after_landing_failure(rows, arm):
    """The first Play in a log must come after the analysis landed, or a played frame showed the baseline look.

    The toggle route ends with look_assist.toggle.end. The clip-load route has no such line, and its result line is the
    last thing the analysis writes.
    """
    landed = False
    for number, _stamp, event, fields in rows:
        if event == "look_assist.apply.result" or (event == "look_assist.toggle.end" and fields.get("checked") == "1"):
            landed = True
        if event == "play.toggled.begin" and fields.get("checked") == "1":
            if not landed:
                return "%s: first play.toggled at line %d comes before the analysis landed" % (arm, number)
            return None
    return None


def _stamp_ms(stamp):
    return datetime.strptime(stamp.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000.0


def _int_or_none(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _gap_ms(start, end):
    try:
        return round(_stamp_ms(end) - _stamp_ms(start), 1)
    except ValueError:
        return None


def measures(rows):
    """The numbers a table needs: wait_ms, the apply.timing fields, and the control block gap (None when unreadable)."""
    out = {}
    apply_stamp = None
    for _number, stamp, event, fields in rows:
        if event == "gui_smoke.look_assist_settle" and "wait_ms" not in out:
            out["wait_ms"] = _int_or_none(fields.get("wait_ms"))
        elif event == "look_assist.apply.timing" and "timing" not in out:
            out["timing"] = dict(fields)
        elif event in ("look_assist.toggle.frame_ready_apply", "look_assist.setSliders.frame_ready_apply") and apply_stamp is None:
            apply_stamp = stamp
        elif event == "look_assist.apply.result" and apply_stamp is not None and "result_gap_ms" not in out:
            out["result_gap_ms"] = _gap_ms(apply_stamp, stamp)
        elif event == "look_assist.toggle.end" and apply_stamp is not None and "toggle_gap_ms" not in out:
            out["toggle_gap_ms"] = _gap_ms(apply_stamp, stamp)
    return out


def run(control_text, treatment_text, require_walk=False, require_surface_search=False):
    control = parse_log(control_text)
    treatment = parse_log(treatment_text)
    failures = []
    for rows, arm in ((control, "control"), (treatment, "treatment")):
        failures.extend(requirement_failures(rows, arm, require_walk, require_surface_search))
        play = play_after_landing_failure(rows, arm)
        if play:
            failures.append(play)
    differences = compare_rows(control, treatment)
    return {
        "verdict": "identical" if not failures and not differences else "differs",
        "failures": failures,
        "differences": differences,
        "control": measures(control),
        "treatment": measures(treatment),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("control", help="control arm smoke-run.log")
    parser.add_argument("treatment", help="treatment arm smoke-run.log")
    parser.add_argument("--require-walk", action="store_true", help="fail if either arm has post_walk_ran=0")
    parser.add_argument("--require-surface-search", action="store_true", help="fail unless both arms have surface_search=converged")
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args(argv)
    try:
        with open(args.control, encoding="utf-8", errors="replace") as handle:
            control_text = handle.read()
        with open(args.treatment, encoding="utf-8", errors="replace") as handle:
            treatment_text = handle.read()
    except OSError as error:
        print("UNREADABLE %s" % error, file=sys.stderr)
        return 2
    report = run(control_text, treatment_text, args.require_walk, args.require_surface_search)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(report["verdict"])
        for line in report["failures"] + report["differences"]:
            print("  " + line)
    return 0 if report["verdict"] == "identical" else 1


if __name__ == "__main__":
    sys.exit(main())
