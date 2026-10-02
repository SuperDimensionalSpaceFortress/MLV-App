#!/usr/bin/env python3
"""Per-render-state gate for Look Assist on the tracked DAYLIGHT fixtures, from real-app profile runs.

A run here is the app's decode-only profile mode with the Look Assist settle exercise, on a tracked fixture
(no Play action, no loop; the hub sanctioned it for this measurement only), sync (default) and async
(MLVAPP_LOOK_ASSIST_ASYNC=1). Each run directory is named
`<tag>-<clip>-<mode>` and holds `logs/*.log` (the interaction trace) and, when the build supports
MLVAPP_LOOK_ASSIST_PICTURE_DUMP, `pic/look-assist-final-frame0.png` (the app's own render at the applied look)
and `pic/state-master.png` (the same app rendering master's logged look in the same state). The gate:

  * every arm settled on the daylight scene ("shade") with a balance solved from, or refined on, the RENDERED
    picture -- never "as-shot-prior" at the base balance (PR #221 r2: deck chroma 18.9 in the real app against
    master's 13.5), and inside the daylight window;
  * sync and async land on the SAME receipt (temperature, tint, exposure) for each clip;
  * the picture, when present: the deck chroma of the applied look is <= --deck-chroma-max AND <= the deck
    chroma of master's look rendered by the same app in the same state.

stdlib only for the decision half; the picture half needs Pillow (as check_look_assist_sidecars.py).
usage: check_look_assist_profile_states.py <root> --tag TAG [--deck-chroma-max X]
exit 0 = pass, 1 = a gate failed, 2 = nothing usable (no run directories / Pillow missing for the picture gate).
"""
import argparse
import glob
import os
import re
import sys

import check_look_assist_sidecars as sidecars

RESULT = re.compile(r"look_assist\.apply\.result\b.*?\bscene=(\S+).*?\bpreset_exp=(-?\d+).*?\bfinal_temp=(-?\d+) final_tint=(-?\d+)")
ASYNC_APPLIED = re.compile(r"look_assist\.apply\.auto_wb_async_applied\b.*?\bsource=(\S+) decision=(\S+).*?\bfinal_temp=(-?\d+) final_tint=(-?\d+) preset_exp=(-?\d+)")
AUTO_WB = re.compile(r"look_assist\.apply\.auto_wb\b.*?\bvalid=(\d) source=(\S+) decision=(\S+)")
ASYNC_DISPATCH = re.compile(r"look_assist\.apply\.async_dispatch\b.*?\bscene=(\S+)")


def parse_log_text(text):
    """-> dict(scene, source, decision, temperature, tint, exposure) for the LAST settled apply, or None."""
    receipt = None
    for line in text.splitlines():
        wb = AUTO_WB.search(line)
        if wb:
            receipt = dict(receipt or {}, source=wb.group(2), decision=wb.group(3))
            continue
        result = RESULT.search(line)
        if result:
            receipt = dict(receipt or {}, scene=result.group(1), exposure=int(result.group(2)),
                           temperature=int(result.group(3)), tint=int(result.group(4)))
            continue
        dispatch = ASYNC_DISPATCH.search(line)
        if dispatch:
            receipt = dict(receipt or {}, scene=dispatch.group(1))
            continue
        applied = ASYNC_APPLIED.search(line)
        if applied:
            receipt = dict(receipt or {}, source=applied.group(1), decision=applied.group(2),
                           temperature=int(applied.group(3)), tint=int(applied.group(4)), exposure=int(applied.group(5)))
    return receipt


def read_run(directory):
    text = ""
    for path in sorted(glob.glob(os.path.join(directory, "logs", "*.log"))):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text += handle.read() + "\n"
    return parse_log_text(text)


def check_receipt(name, receipt):
    failures = []
    if not receipt or not all(k in receipt for k in ("scene", "source", "decision", "temperature", "tint", "exposure")):
        return ["%s: no complete Look Assist apply in the trace (did the run settle?): %r" % (name, receipt)]
    if receipt["scene"] != "shade":
        failures.append("%s: scene=%r, want 'shade' (daylight fixture)" % (name, receipt["scene"]))
    if receipt["source"] not in sidecars.ALLOWED_WB_SOURCES:
        failures.append("%s: wb source=%r, want one of %s (as-shot-prior at the base balance is the regression)"
                        % (name, receipt["source"], sidecars.ALLOWED_WB_SOURCES))
    if receipt["decision"] not in sidecars.ACCEPTED_WB_DECISIONS:
        failures.append("%s: wb decision=%r, want one of %s" % (name, receipt["decision"], sidecars.ACCEPTED_WB_DECISIONS))
    lo, hi = sidecars.DAYLIGHT_TEMPERATURE
    if not lo <= receipt["temperature"] <= hi:
        failures.append("%s: temperature %d outside the daylight window %s" % (name, receipt["temperature"], (lo, hi)))
    lo, hi = sidecars.DAYLIGHT_TINT
    if not lo <= receipt["tint"] <= hi:
        failures.append("%s: tint %d outside the daylight window %s" % (name, receipt["tint"], (lo, hi)))
    return failures


def check_parity(clip, sync, async_):
    keys = ("temperature", "tint", "exposure")
    if not sync or not async_:
        return []
    if any(sync.get(k) != async_.get(k) for k in keys):
        return ["%s: sync %s != async %s (the two GUI paths must land on the same receipt)"
                % (clip, tuple(sync.get(k) for k in keys), tuple(async_.get(k) for k in keys))]
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--deck-chroma-max", type=float, default=None)
    args = parser.parse_args(argv)

    runs = {}
    for directory in sorted(glob.glob(os.path.join(args.root, args.tag + "-*"))):
        base = os.path.basename(directory)
        match = re.match(re.escape(args.tag) + r"-(.+)-(sync|async)$", base)
        if match and os.path.isdir(directory):
            runs[(match.group(1), match.group(2))] = directory
    if not runs:
        print("no %s-<clip>-<sync|async> run directories in %s" % (args.tag, args.root))
        return 2

    failures = []
    receipts = {}
    for (clip, mode), directory in sorted(runs.items()):
        receipts[(clip, mode)] = read_run(directory)
        failures += check_receipt("%s/%s" % (clip, mode), receipts[(clip, mode)])
    for clip in sorted({c for c, _ in runs}):
        failures += check_parity(clip, receipts.get((clip, "sync")), receipts.get((clip, "async")))

    notes = []
    if args.deck_chroma_max is not None:
        for (clip, mode), directory in sorted(runs.items()):
            applied = os.path.join(directory, "pic", "look-assist-final-frame0.png")
            if not os.path.exists(applied):
                continue   # the async arm of a build with no async dump, etc.
            try:
                chroma = sidecars.deck_cast_chroma(applied)
                master_png = os.path.join(directory, "pic", "state-master.png")
                master = sidecars.deck_cast_chroma(master_png) if os.path.exists(master_png) else None
            except ImportError:
                print("Pillow is required for --deck-chroma-max")
                return 2
            notes.append("%s/%s deck chroma %.2f%s" % (clip, mode, chroma, "" if master is None else " (master look %.2f)" % master))
            if chroma > args.deck_chroma_max:
                failures.append("%s/%s: deck chroma %.2f > %.2f" % (clip, mode, chroma, args.deck_chroma_max))
            if master is not None and chroma > master:
                failures.append("%s/%s: deck chroma %.2f is worse than master's look in the same state (%.2f)"
                                % (clip, mode, chroma, master))
    for failure in failures:
        print("FAIL " + failure)
    if not failures:
        print("PASS %d run(s): %s" % (len(runs), "; ".join(notes) if notes else "decisions + sync/async parity"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
