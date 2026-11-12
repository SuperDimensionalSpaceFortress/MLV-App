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

  * every arm ended on a settled receipt: no skip event ANYWHERE in the arm's log (before or after the result, with
    or without reason=, whatever a later fallback / dispatch rebuilt), not a safety fallback, and not an EARLIER accepted
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
  Picture binding (793-I2): the applied picture is the file the newest look_assist.apply.picture_dump line of the arm's
  log names (final=look-assist-final-frameN.png final_sha256=<hex> final_exposure=<n> frame=<n>), that line belongs to the
  LAST application (it precedes that application's result line), final=none is STALE (the file on disk is another run's),
  the SHA-256 of the PNG bytes equals final_sha256, and the frame and exposure match the application. An arm with no
  picture_dump line at all is UNBOUND_LEGACY: a failure unless --allow-unbound-legacy, which reports it as such, never PASS.
  A log line is the event named by its first `interaction_trace event=` token, its fields are its whitespace-delimited
  key=value pairs taken exactly once (integers -?[0-9]{1,9}); an incomplete or ambiguous result is "no complete apply", and
  a line with no event marker keeps its decision reading but never owns a picture_dump line.

usage: check_look_assist_profile_states.py <root> --tag TAG [--clips a,b] [--deck-chroma-max X] [--require-daylight]
           [--master-tolerance X] [--subject-sha SHA] [--exe PATH] [--decisions-only] [--allow-unbound-legacy]
exit 0 = pass, 1 = a gate failed, 2 = nothing usable (no run directories / unreadable picture).
"""
import argparse
import glob
import hashlib
import os
import re
import sys

import check_look_assist_sidecars as sidecars
import look_assist_png
import verify_exe_stamp

DEFAULT_CLIPS = ("tiny_dual_iso", "large_dual_iso")
MODES = ("sync", "async")
APPLIED_PICTURE = os.path.join("pic", "look-assist-final-frame0.png")
MASTER_PICTURE = os.path.join("pic", "state-master.png")

# A line IS the event named by its first `interaction_trace event=` token (EVENT below), and only that event's reading
# applies to it: a name spelled out inside another event's field is no evidence (adversary r2 A3). A line with NO such
# marker (event=None, the back-compat reading) is read by the name appearing in it: it keeps its decision reading but
# never owns a picture_dump line (adversary r2 A1). The fields of a line are its whitespace-delimited key=value pairs,
# each taken exactly once, integers as -?[0-9]{1,9}; anything else is "no complete apply" (fail closed), never the
# parse of a prefix or a shadow token (adversary r2 A2/A6).
RESULT_EVENT = "look_assist.apply.result"
ASYNC_APPLIED_EVENT = "look_assist.apply.auto_wb_async_applied"
AUTO_WB_EVENT = "look_assist.apply.auto_wb"
ASYNC_DISPATCH_EVENT = "look_assist.apply.async_dispatch"
SAFETY_FALLBACK_EVENT = "look_assist.apply.safety_fallback"
# EVERY skip event, with or without a reason: the app's env-disabled / file / receipt / enabled skip is logged as
# "file_loaded=1 mlv=1 receipt=1 enabled=1" and has no reason= at all (sol, PR #222 r2).
SKIP_EVENT = "look_assist.apply.skip"
FALLBACK_EVENT = "look_assist.daylight_fallback_to_master"
EVENT_NAME = {name: re.compile(re.escape(name) + r"\b") for name in (
    RESULT_EVENT, ASYNC_APPLIED_EVENT, AUTO_WB_EVENT, ASYNC_DISPATCH_EVENT, SAFETY_FALLBACK_EVENT, SKIP_EVENT, FALLBACK_EVENT)}
INT_FIELD = re.compile(r"-?[0-9]{1,9}")
DAMPING_FIELD = re.compile(r"-?[0-9]{1,9}\.[0-9]{3}")       # the app logs damping as %.3f (MainWindow.cpp, .arg(damping, 0, 'f', 3))
# What one application's result sets, plus the two markers of its identity: "closed" (a result / applied line completed it, so
# the next one must bring its own evidence, never borrow this one's) and "generation" (the dispatch's, which the applied event
# of the same application must repeat). A new application (auto_wb, dispatch) starts without any of them.
RESULT_KEYS = ("scene", "exposure", "temperature", "tint", "frame", "picture_dump", "source", "decision", "closed", "generation", "dispatch_frame")
BUILD_SHA = re.compile(r'"build_sha"\s*:\s*"([^"]*)"')
# The app's own binding of the final PNG (PR #793 head cc3205de, src/batch/LookAssistAnalysis.cpp lookAssistPictureDumpFields):
#   look_assist.apply.picture_dump pre_safety=<name|none> pre_safety_sha256=<hex|none> pre_safety_exposure=<n>
#       final=<look-assist-final-frameN.png|none> final_sha256=<hex of the exact PNG bytes|none> final_exposure=<n>
#       safety=<keep|retreat|revert> frame=<n>
# It is logged after the application's last look change and BEFORE that application's look_assist.apply.result line.
TRACE_TAG = re.compile(r"(?<!\S)interaction_trace(?!\S)")      # a standalone token (not a suffix or a field value); the FIRST is the marker
TRACE_EVENT = re.compile(r"\s+event=(\S*)")           # what must follow it; an empty or absent event is NO event, never the next marker


def line_event(line):
    """-> (event, fields_start). event is None for a line with no interaction_trace tag at all (the old back-compat
    reading), and "" for a tag not followed by a non-empty event=: such a line is no event of ours."""
    tag = TRACE_TAG.search(line)
    if not tag:
        return None, 0
    named = TRACE_EVENT.match(line, tag.end())
    # The fields start AT the tag, so the event= selector is itself one of the line's key=value tokens: a second event= (any
    # spelling) is a duplicate like any other and the line is ambiguous (adversary pass 978b1ac0 A1).
    return (named.group(1) if named else ""), tag.end()
DUMP_EVENT = "look_assist.apply.picture_dump"
# EVERY name=value token the line carries, whatever the spelling of the name (upper case, hyphens, anything but "=" and
# whitespace): the duplicate check runs over all of them BEFORE any consumer looks up the lower-case names it knows, so a
# repeated "Audit=" or "audit-note=" is as ambiguous as a repeated "audit=" (sol, PR #799 r3 INV-1).
FIELD = re.compile(r"(?<!\S)([^\s=]+)=(\S*)")
DIGITS = re.compile(r"[0-9]{1,9}")      # a frame number; never an arbitrarily long digit string
FINAL_PICTURE_NAME = re.compile(r"^look-assist-final-frame(\d+)\.png$")


def has_duplicate_key(pairs):
    """True when one name is given twice, spelled exactly the same OR the same after case-folding."""
    names = [key for key, _ in pairs]
    return len(set(names)) != len(names) or len({key.casefold() for key in names}) != len(names)


def open_base(receipt):
    """What a result / applied event may build on: the receipt a dispatch or auto_wb opened. A receipt a result or applied
    event already completed is CLOSED: the next such event starts from nothing and must bring its own scene, source and
    decision, or it is "no complete apply" -- never a composite of two applications (adversary r3 A1)."""
    receipt = receipt or {}
    return {} if receipt.get("closed") else receipt


def parse_log_text(text):
    """-> dict(scene, source, decision, temperature, tint, exposure[, fell_back]) for the LAST application, or None.

    One application is what a dispatch / fallback / result sequence builds. A later event that starts a NEW
    application (an async dispatch, a fall-back to master) discards what an earlier one left, and a safety
    fallback replaces it with a failure marker: an accepted receipt from before is not the shipped picture any
    more. A skip is different: it invalidates the ARM, not the receipt in hand. It is kept apart from the receipt
    (which the fallback / dispatch / result events below replace) and stamped back on at the end, so no later
    event -- a master fallback, an async dispatch, a complete application -- can clear it (sol, PR #224 r1).
    """
    receipt = None
    skipped = None
    pending_dump = None      # the picture_dump line the NEXT result will own (it precedes its own result line)
    newest_dump = None       # the newest picture_dump line anywhere in the log
    dump_lines = 0
    for line in text.splitlines():
        event, start = line_event(line)
        marked = event is not None          # False: no interaction_trace tag (back-compat reading, never owns a dump)
        pairs = FIELD.findall(line[start:])
        ambiguous = has_duplicate_key(pairs)      # a key given twice (in any spelling): no field of the line is trusted

        def is_event(name):
            return event == name if marked else bool(EVENT_NAME[name].search(line))

        def once(key):
            values = [value for found, value in pairs if found == key]
            return values[0] if len(values) == 1 and not ambiguous else None

        def int_once(key):
            value = once(key)
            return int(value) if value is not None and INT_FIELD.fullmatch(value) else None

        if is_event(SAFETY_FALLBACK_EVENT):
            receipt = {"safety_fallback": True}
            pending_dump = None
            continue
        if is_event(SKIP_EVENT):
            skipped = skipped or once("reason") or "no reason logged"   # arm-level marker: the first skip is named
            continue
        if event == DUMP_EVENT:
            fields = dict(pairs)
            if has_duplicate_key(pairs):
                fields["_duplicate_key"] = "1"      # a key given twice is ambiguous, never a binding
            pending_dump = newest_dump = fields
            dump_lines += 1
            continue
        if is_event(FALLBACK_EVENT):
            receipt = {"fell_back": True}
            pending_dump = None
            continue
        if is_event(ASYNC_DISPATCH_EVENT):
            scene, generation = once("scene"), once("generation")
            # a new application: nothing of the previous one survives. Its generation (None when it logs none) is what its
            # applied event must repeat: another generation's application is not this dispatch's (adversary r3 A2). The
            # app always logs the analysed frame as an integer (adversary r3 A1, pass on 5cb9b1a9).
            frame = once("frame")
            if (scene and frame is not None and INT_FIELD.fullmatch(frame) and (generation is None or INT_FIELD.fullmatch(generation))
                    and once("floor_lifted") in ("0", "1")):      # bool01, always logged by the dispatch
                receipt = {"scene": scene, "generation": generation, "dispatch_frame": frame}      # the applied event repeats both
            else:
                receipt = {"malformed_event": ASYNC_DISPATCH_EVENT}
            pending_dump = None
            continue
        if is_event(AUTO_WB_EVENT):
            valid, source, decision = once("valid"), once("source"), once("decision")
            damping = once("damping")      # always logged, as %.3f
            if valid not in ("0", "1") or not source or not decision or damping is None or not DAMPING_FIELD.fullmatch(damping):      # bool01: 0 or 1
                receipt = {"malformed_event": AUTO_WB_EVENT}
            else:
                base = receipt or {}
                if "exposure" in base:      # a complete application stands behind us: this auto_wb begins a NEW one, which
                    base = {k: v for k, v in base.items() if k not in RESULT_KEYS}      # inherits none of its result
                receipt = dict(base, source=source, decision=decision)
                receipt.pop("malformed_event", None)
            receipt.pop("picture_dump", None)        # a new application began: the earlier result's dump is not its
            pending_dump = None                     # auto_wb precedes the dump of its own application
            continue
        if is_event(RESULT_EVENT):
            scene, exposure, temperature, tint = once("scene"), int_once("preset_exp"), int_once("final_temp"), int_once("final_tint")
            if not scene or None in (exposure, temperature, tint):
                receipt = {"malformed_event": RESULT_EVENT}     # an incomplete result is no apply, never an earlier receipt
                pending_dump = None
                continue
            receipt = dict(open_base(receipt), scene=scene, exposure=exposure, temperature=temperature, tint=tint, closed=True)
            receipt.pop("malformed_event", None)
            receipt["frame"] = [value for key, value in pairs if key == "frame"]    # this event's own body fields; validated at the binding
            if marked:
                receipt["picture_dump"], pending_dump = pending_dump, None    # this application owns the dump before it
            else:
                receipt["picture_dump"], pending_dump = None, None    # no event marker: decisions only; it owns no dump and
            continue                                                  # a dump it ends is spent, never inherited by a later result
        if is_event(ASYNC_APPLIED_EVENT):
            source, decision = once("source"), once("decision")
            temperature, tint, exposure = int_once("final_temp"), int_once("final_tint"), int_once("preset_exp")
            base = open_base(receipt)
            valid = once("valid")      # the 0/1 flag the app logs (bool01), as auto_wb's; and the awb_* pair, as integers
            if (not source or not decision or None in (temperature, tint, exposure)
                    or valid not in ("0", "1") or None in (int_once("awb_temp"), int_once("awb_tint"))
                    or once("damping") is None or not DAMPING_FIELD.fullmatch(once("damping"))
                    or base.get("dispatch_frame") != once("frame")      # the frame its own dispatch analysed, each given once
                    or base.get("generation") != once("generation")):      # the generation of ITS OWN dispatch, each given once
                receipt = {"malformed_event": ASYNC_APPLIED_EVENT}
                pending_dump = None
                continue
            receipt = dict(base, source=source, decision=decision, temperature=temperature, tint=tint, exposure=exposure, closed=True)
            receipt.pop("malformed_event", None)
            receipt["frame"] = [value for key, value in pairs if key == "frame"]    # this event's own body fields; validated at the binding
            if marked:
                receipt["picture_dump"], pending_dump = pending_dump, None
            else:
                receipt["picture_dump"], pending_dump = None, None
    if skipped:
        receipt = dict(receipt or {}, skipped=skipped)
    if receipt is not None and dump_lines:
        # Only the NEWEST picture_dump line can bind the final PNG now on disk, and only when it belongs to the LAST
        # application. receipt["picture_dump"] is None when the last application owns no dump line.
        receipt["picture_dump_lines"] = dump_lines
        receipt["picture_dump_is_newest"] = receipt.get("picture_dump") is newest_dump
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


def check_picture_binding(name, receipt, png_path, png_bytes, allow_legacy=False):
    """-> (state, failures); png_bytes are the bytes of png_path that are also measured (one read). state is 'bound' (the newest picture_dump line names exactly this PNG, by content, for the
    last application), 'legacy' (the arm's log has no picture_dump line at all: UNBOUND_LEGACY, a failure unless
    allow_legacy), 'stale' (a picture_dump line exists and does not bind this PNG) or None (no usable receipt: the
    decision gate already failed the arm). 793-I2: a readable PNG left by an earlier run must not pass."""
    if not receipt or not all(k in receipt for k in ("scene", "source", "decision", "temperature", "tint", "exposure")):
        return None, []
    lines = receipt.get("picture_dump_lines", 0)
    if not lines:
        if allow_legacy:
            return "legacy", []
        return "legacy", ["%s: UNBOUND_LEGACY - the log has no look_assist.apply.picture_dump line, so %s is not tied to this "
                          "application (only --allow-unbound-legacy accepts that, and reports it as UNBOUND_LEGACY, never as PASS)"
                          % (name, os.path.basename(png_path))]
    dump = receipt.get("picture_dump")
    if dump is None or not receipt.get("picture_dump_is_newest"):
        return "stale", ["%s: STALE - the newest look_assist.apply.picture_dump line does not belong to the last application "
                         "(it precedes an earlier application's result or follows the last result), so %s is not its picture"
                         % (name, os.path.basename(png_path))]
    missing = [k for k in ("final", "final_sha256", "final_exposure", "frame") if k not in dump]
    if missing:
        return "stale", ["%s: STALE - the picture_dump line lacks %s" % (name, ", ".join(missing))]
    if dump["final"] == "none" or dump["final_sha256"] == "none":
        return "stale", ["%s: STALE - the picture_dump line says final=none: the app wrote no %s for this application, so a "
                         "readable file of that name is left over from another run" % (name, os.path.basename(png_path))]
    wanted = FINAL_PICTURE_NAME.match(os.path.basename(png_path))
    named = FINAL_PICTURE_NAME.match(dump["final"])
    failures = []
    if dump.get("_duplicate_key"):
        failures.append("%s: STALE - the picture_dump line gives a key twice" % name)
    if not named or dump["final"] != os.path.basename(png_path):
        failures.append("%s: STALE - picture_dump names final=%r, the gate reads %s" % (name, dump["final"], os.path.basename(png_path)))
    result_frames = receipt.get("frame") or []
    if not DIGITS.fullmatch(dump["frame"]) or (wanted and wanted.group(1) != dump["frame"]) or (named and named.group(1) != dump["frame"]):
        failures.append("%s: STALE - picture_dump frame=%s does not match the picture under test (%s)"
                        % (name, dump["frame"], os.path.basename(png_path)))
    elif len(result_frames) != 1 or not DIGITS.fullmatch(result_frames[0]) or int(result_frames[0]) != int(dump["frame"]):
        failures.append("%s: STALE - the application's result must carry exactly one frame=<digits> equal to picture_dump "
                        "frame=%s (got %r)" % (name, dump["frame"], result_frames))
    if dump["final_exposure"] != str(receipt["exposure"]):
        failures.append("%s: STALE - picture_dump final_exposure=%s but the application's receipt exposure is %d"
                        % (name, dump["final_exposure"], receipt["exposure"]))
    actual = hashlib.sha256(png_bytes).hexdigest()
    if actual != dump["final_sha256"].lower():
        failures.append("%s: STALE - sha256 of %s is %s, picture_dump final_sha256=%s (the file is not the picture the app wrote)"
                        % (name, os.path.basename(png_path), actual, dump["final_sha256"]))
    return ("stale" if failures else "bound"), failures


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
    parser.add_argument("--allow-unbound-legacy", action="store_true",
                        help="accept an arm whose log has no look_assist.apply.picture_dump line (evidence from a build that "
                             "does not emit it): reported as UNBOUND_LEGACY, never as PASS. A line that is present and does "
                             "not bind the picture is STALE and always fails.")
    args = parser.parse_args(argv)

    runs = find_runs(args.root, args.tag)
    if not runs:
        print("no %s-<clip>-<sync|async> run directories in %s" % (args.tag, args.root))
        return 2

    failures = []
    notes = []
    legacy = []
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
                with open(applied_png, "rb") as handle:
                    applied_bytes = handle.read()        # ONE read: the bytes hashed below are the bytes measured here
                chroma = look_assist_png.deck_cast_chroma(applied_png, applied_bytes)
                master = sidecars.deck_cast_chroma(master_png)
            except (OSError, ValueError) as error:
                print("cannot read a picture of %s: %s" % (name, error))
                return 2
            klass = receipt_class(receipts[(clip, mode)]) or "?"
            tolerance = args.master_tolerance if klass == "master" else 0.0
            state, binding_failures = check_picture_binding(name, receipts[(clip, mode)], applied_png, applied_bytes, args.allow_unbound_legacy)
            failures += binding_failures
            if state == "legacy":
                legacy.append(name)
            notes.append("%s [%s] deck chroma %.2f (master's look %.2f)%s"
                         % (name, klass, chroma, master, {"bound": " picture_dump-bound", "legacy": " UNBOUND_LEGACY"}.get(state, "")))
            if args.deck_chroma_max is not None and chroma > args.deck_chroma_max:
                failures.append("%s: deck chroma %.2f > %.2f" % (name, chroma, args.deck_chroma_max))
            if chroma > master + tolerance:
                failures.append("%s: deck chroma %.2f is worse than master's look in the same state (%.2f%s)"
                                % (name, chroma, master, "" if not tolerance else " + %.2f tolerance" % tolerance))

    for failure in failures:
        print("FAIL " + failure)
    if not failures and legacy:
        # Accepted only by --allow-unbound-legacy, and never labelled PASS: the picture is not tied to the application.
        print("UNBOUND_LEGACY-ACCEPTED %d run(s), %d arm(s) with no picture_dump binding (%s): %s"
              % (len(runs), len(legacy), ", ".join(legacy), "; ".join(notes)))
    elif not failures:
        print("PASS %d run(s): %s" % (len(runs), "; ".join(notes) if notes else "decisions + sync/async parity"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
