#!/usr/bin/env python3
"""Per-render-state gate for Look Assist on the tracked DAYLIGHT fixtures, from real-app profile runs (stdlib only).

A run here is the app's decode-only profile mode with the Look Assist settle exercise, on a tracked fixture
(no Play action, no loop; the hub sanctioned it for this measurement only), sync (default) and async
(MLVAPP_LOOK_ASSIST_ASYNC=1). Each run directory is named `<tag>-<clip>-<mode>` and holds `logs/*.log` (the
interaction trace) and `pic/look-assist-final-frame0.png` (the app's own render at the applied look) and
`pic/state-master.png` (the same app rendering MASTER's logged look in the same state; the exact environment and
argv of every picture are recorded in each run's COMMANDS.txt and, for the recorded run CI replays, in
tests/fixtures/look_assist_profile_runs/pr222-r2/MANIFEST.json). The gate -- the class rule is "never worse
than master in any render state":

  * every arm ended on a settled receipt: not skipped, not a safety fallback, and not an EARLIER accepted
    receipt that a later failed application left standing (sol, PR #222 r1);
  * the receipt is one of two things. DAYLIGHT: scene "shade", a balance solved from / refined on the RENDERED
    picture ("processed-neutral-patch" / "rendered-neutral-patch", accepted), inside the daylight window. Or
    MASTER's own analysis: scene not "shade" (the daylight verdict was not trusted -- nothing verified a
    surface -- and the clip was re-analysed as master analyses it). The half-states never pass: "as-shot-prior"
    at the base balance (PR #221 r2: deck chroma 18.9 against master's 13.5) and "master-balance" under a shade
    scene (PR #222 r1: master's balance on the daylight preset, the same picture);
  * sync and async land on the SAME receipt (temperature, tint, exposure) for each clip, and BOTH arms exist
    (a missing partner is a failure, not a skipped comparison);
  * the pictures exist for every arm (missing ones are a failure) and the deck chroma of the applied look is <=
    the deck chroma of master's look rendered by the same app in the same state (a master-analysis arm is master's
    look rendered by two paths, so it gets --master-tolerance) and <= --deck-chroma-max when given;
  * --subject-sha: every arm's log records the build it ran under (run_metadata "build_sha") and it is the subject;
    --exe: the executable carries the subject's build stamp (verify_exe_stamp.py).
  --require-daylight turns the master-analysis outcome into a failure: the improvement gate, not the safety one.

usage: check_look_assist_profile_states.py <root> --tag TAG [--clips a,b] [--deck-chroma-max X] [--require-daylight]
           [--master-tolerance X] [--subject-sha SHA] [--exe PATH] [--decisions-only]
exit 0 = pass, 1 = a gate failed, 2 = nothing usable (no run directories / unreadable picture).
"""
import argparse
import glob
import os
import re
import sys

import check_look_assist_sidecars as sidecars
import verify_exe_stamp

DEFAULT_CLIPS = ("tiny_dual_iso", "large_dual_iso")
MODES = ("sync", "async")
APPLIED_PICTURE = os.path.join("pic", "look-assist-final-frame0.png")
MASTER_PICTURE = os.path.join("pic", "state-master.png")

RESULT = re.compile(r"look_assist\.apply\.result\b.*?\bscene=(\S+).*?\bpreset_exp=(-?\d+).*?\bfinal_temp=(-?\d+) final_tint=(-?\d+)")
ASYNC_APPLIED = re.compile(r"look_assist\.apply\.auto_wb_async_applied\b.*?\bsource=(\S+) decision=(\S+).*?\bfinal_temp=(-?\d+) final_tint=(-?\d+) preset_exp=(-?\d+)")
AUTO_WB = re.compile(r"look_assist\.apply\.auto_wb\b.*?\bvalid=(\d) source=(\S+) decision=(\S+)")
ASYNC_DISPATCH = re.compile(r"look_assist\.apply\.async_dispatch\b.*?\bscene=(\S+)")
SAFETY_FALLBACK = re.compile(r"look_assist\.apply\.safety_fallback\b")
APPLY_SKIP = re.compile(r"look_assist\.apply\.skip\b.*?\breason=(\S+)")
FALLBACK_TO_MASTER = re.compile(r"look_assist\.daylight_fallback_to_master\b")
BUILD_SHA = re.compile(r'"build_sha"\s*:\s*"([^"]*)"')


def parse_log_text(text):
    """-> dict(scene, source, decision, temperature, tint, exposure[, fell_back]) for the LAST application, or None.

    One application is what a dispatch / fallback / result sequence builds. A later event that starts a NEW
    application (an async dispatch, a fall-back to master) discards what an earlier one left, and a safety
    fallback or a skip replaces it with a failure marker: an accepted receipt from before is not the shipped
    picture any more.
    """
    receipt = None
    for line in text.splitlines():
        if SAFETY_FALLBACK.search(line):
            receipt = {"safety_fallback": True}
            continue
        skip = APPLY_SKIP.search(line)
        if skip:
            receipt = {"skipped": skip.group(1)}
            continue
        if FALLBACK_TO_MASTER.search(line):
            receipt = {"fell_back": True}
            continue
        dispatch = ASYNC_DISPATCH.search(line)
        if dispatch:
            receipt = {"scene": dispatch.group(1)}   # a new application: nothing of the previous one survives
            continue
        wb = AUTO_WB.search(line)
        if wb:
            receipt = dict(receipt or {}, source=wb.group(2), decision=wb.group(3))
            continue
        result = RESULT.search(line)
        if result:
            receipt = dict(receipt or {}, scene=result.group(1), exposure=int(result.group(2)),
                           temperature=int(result.group(3)), tint=int(result.group(4)))
            continue
        applied = ASYNC_APPLIED.search(line)
        if applied:
            receipt = dict(receipt or {}, source=applied.group(1), decision=applied.group(2),
                           temperature=int(applied.group(3)), tint=int(applied.group(4)), exposure=int(applied.group(5)))
    return receipt


def read_log_text(directory):
    text = ""
    for path in sorted(glob.glob(os.path.join(directory, "logs", "*.log"))):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text += handle.read() + "\n"
    return text


def read_run(directory):
    return parse_log_text(read_log_text(directory))


def receipt_class(receipt):
    """'daylight' | 'master' | None (not a valid outcome)."""
    if not receipt:
        return None
    if receipt.get("scene") == "shade":
        return "daylight"
    return "master"


def check_receipt(name, receipt, require_daylight=False):
    failures = []
    if receipt and receipt.get("safety_fallback"):
        return ["%s: the last application ended in a safety fallback (an earlier accepted receipt is not the shipped picture)" % name]
    if receipt and receipt.get("skipped"):
        return ["%s: the last application was skipped (%s)" % (name, receipt["skipped"])]
    if not receipt or not all(k in receipt for k in ("scene", "source", "decision", "temperature", "tint", "exposure")):
        return ["%s: no complete Look Assist apply in the trace (did the run settle?): %r" % (name, receipt)]
    if receipt["source"] in sidecars.FORBIDDEN_WB_SOURCES:
        failures.append("%s: wb source=%r is a half-state, never an outcome (as-shot-prior at the base balance, or master's "
                        "balance on the daylight preset)" % (name, receipt["source"]))
    if receipt["scene"] == "shade":
        if receipt["source"] not in sidecars.ALLOWED_WB_SOURCES:
            failures.append("%s: shade scene with wb source=%r, want one of %s (or master's own analysis: a scene other than shade)"
                            % (name, receipt["source"], sidecars.ALLOWED_WB_SOURCES))
        if receipt["decision"] not in sidecars.ACCEPTED_WB_DECISIONS:
            failures.append("%s: wb decision=%r, want one of %s" % (name, receipt["decision"], sidecars.ACCEPTED_WB_DECISIONS))
        lo, hi = sidecars.DAYLIGHT_TEMPERATURE
        if not lo <= receipt["temperature"] <= hi:
            failures.append("%s: temperature %d outside the daylight window %s" % (name, receipt["temperature"], (lo, hi)))
        lo, hi = sidecars.DAYLIGHT_TINT
        if not lo <= receipt["tint"] <= hi:
            failures.append("%s: tint %d outside the daylight window %s" % (name, receipt["tint"], (lo, hi)))
    elif require_daylight:
        failures.append("%s: master's own analysis (scene=%r), not a daylight balance (--require-daylight)" % (name, receipt["scene"]))
    return failures


def check_parity(clip, sync, async_):
    keys = ("temperature", "tint", "exposure")
    if not sync or not async_:
        return []
    if any(sync.get(k) != async_.get(k) for k in keys):
        return ["%s: sync %s != async %s (the two GUI paths must land on the same receipt)"
                % (clip, tuple(sync.get(k) for k in keys), tuple(async_.get(k) for k in keys))]
    return []


def check_subject(name, directory, subject_sha):
    shas = set(BUILD_SHA.findall(read_log_text(directory)))
    if not shas:
        return ["%s: the log records no build_sha, so the run is not tied to the subject %s" % (name, subject_sha)]
    if shas != {subject_sha}:
        return ["%s: the log's build_sha %s is not the subject %s" % (name, sorted(shas), subject_sha)]
    return []


def find_runs(root, tag):
    runs = {}
    for directory in sorted(glob.glob(os.path.join(root, tag + "-*"))):
        match = re.match(re.escape(tag) + r"-(.+)-(sync|async)$", os.path.basename(directory))
        if match and os.path.isdir(directory):
            runs[(match.group(1), match.group(2))] = directory
    return runs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--clips", default=",".join(DEFAULT_CLIPS), help="clips that must have BOTH a sync and an async run")
    parser.add_argument("--deck-chroma-max", type=float, default=None)
    parser.add_argument("--master-tolerance", type=float, default=0.5,
                        help="a master-analysis arm is master's look rendered by two paths: allow this much deck chroma")
    parser.add_argument("--require-daylight", action="store_true")
    parser.add_argument("--subject-sha", default=None)
    parser.add_argument("--exe", default=None)
    parser.add_argument("--decisions-only", action="store_true", help="skip the picture gate (the pictures are then NOT required)")
    args = parser.parse_args(argv)

    runs = find_runs(args.root, args.tag)
    if not runs:
        print("no %s-<clip>-<sync|async> run directories in %s" % (args.tag, args.root))
        return 2

    failures = []
    notes = []
    for clip in [c for c in args.clips.split(",") if c]:
        for mode in MODES:
            if (clip, mode) not in runs:
                failures.append("%s/%s: no run directory (a missing sync/async partner is a failure, not a skipped comparison)"
                                % (clip, mode))

    receipts = {}
    for (clip, mode), directory in sorted(runs.items()):
        name = "%s/%s" % (clip, mode)
        receipts[(clip, mode)] = read_run(directory)
        failures += check_receipt(name, receipts[(clip, mode)], args.require_daylight)
        if args.subject_sha:
            failures += check_subject(name, directory, args.subject_sha)
    for clip in sorted({c for c, _ in runs}):
        failures += check_parity(clip, receipts.get((clip, "sync")), receipts.get((clip, "async")))

    if args.exe:
        if not args.subject_sha:
            failures.append("--exe needs --subject-sha")
        else:
            ok, message = verify_exe_stamp.check(args.exe, args.subject_sha)
            notes.append("exe: " + message)
            if not ok:
                failures.append("exe: " + message)

    if not args.decisions_only:
        for (clip, mode), directory in sorted(runs.items()):
            name = "%s/%s" % (clip, mode)
            applied_png = os.path.join(directory, APPLIED_PICTURE)
            master_png = os.path.join(directory, MASTER_PICTURE)
            missing = [p for p in (applied_png, master_png) if not os.path.exists(p)]
            if missing:
                failures.append("%s: missing picture(s) %s (the picture gate needs the applied look AND master's look)"
                                % (name, ", ".join(os.path.relpath(p, directory) for p in missing)))
                continue
            try:
                chroma = sidecars.deck_cast_chroma(applied_png)
                master = sidecars.deck_cast_chroma(master_png)
            except (OSError, ValueError) as error:
                print("cannot read a picture of %s: %s" % (name, error))
                return 2
            klass = receipt_class(receipts[(clip, mode)]) or "?"
            tolerance = args.master_tolerance if klass == "master" else 0.0
            notes.append("%s [%s] deck chroma %.2f (master's look %.2f)" % (name, klass, chroma, master))
            if args.deck_chroma_max is not None and chroma > args.deck_chroma_max:
                failures.append("%s: deck chroma %.2f > %.2f" % (name, chroma, args.deck_chroma_max))
            if chroma > master + tolerance:
                failures.append("%s: deck chroma %.2f is worse than master's look in the same state (%.2f%s)"
                                % (name, chroma, master, "" if not tolerance else " + %.2f tolerance" % tolerance))

    for failure in failures:
        print("FAIL " + failure)
    if not failures:
        print("PASS %d run(s): %s" % (len(runs), "; ".join(notes) if notes else "decisions + sync/async parity"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
