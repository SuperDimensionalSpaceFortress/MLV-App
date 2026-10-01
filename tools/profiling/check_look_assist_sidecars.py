#!/usr/bin/env python3
"""GUI-smoke assertion for Look Assist on the tracked DAYLIGHT fixture (stdlib only).

Reads the frame-NN.json sidecars a `--gui-smoke-playback --contact-sheet-dir` run writes and asserts
the two things the classifier fix has to deliver *in the real app*, on the path the user sees:

  * look_assist_scene == "shade"  -- the daylight clip is not read as night, AND
  * look_assist_wb_source is "processed-neutral-patch" (white balance solved from a neutral patch of
    the RENDERED picture) or "as-shot-prior" (the clip's recorded balance, when no patch can be
    trusted) -- never "none". LOOK-ASSIST-SCENE-CLASSIFY-1 r1 had the right scene and source=none:
    no balance was solved at all and the picture went bluer (deck chroma 11.5 -> 22.4).

The picture itself (deck-patch cast chroma) needs pixels and is measured by the real-app sheet
metrics (see the run summary); this script covers the decision half, with no numpy/Pillow so it runs
in the repo's pinned CI environment.

usage: check_look_assist_sidecars.py <contact-sheet-raw-dir> [--min-frames N]
exit 0 = pass, 1 = assertion failed, 2 = no usable sidecars.
"""
import argparse
import glob
import json
import os
import sys

ALLOWED_WB_SOURCES = ("processed-neutral-patch", "as-shot-prior")
DAYLIGHT_TEMPERATURE = (4800, 10000)   # lookAssistWhiteBalanceBounds() for a daylight clip
DAYLIGHT_TINT = (-35, 10)


def load_sidecars(directory):
    sidecars = []
    for path in sorted(glob.glob(os.path.join(directory, "frame-*.json"))):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("saved", True):
            sidecars.append((os.path.basename(path), data))
    return sidecars


def check(sidecars, min_frames=1):
    """Return a list of failure strings (empty = pass)."""
    failures = []
    if len(sidecars) < min_frames:
        failures.append("only %d saved sidecar(s), need %d" % (len(sidecars), min_frames))
    for name, data in sidecars:
        if not data.get("look_assist_enabled", False):
            failures.append("%s: look assist was not enabled" % name)
            continue
        scene = data.get("look_assist_scene")
        if scene != "shade":
            failures.append("%s: look_assist_scene=%r, want 'shade' (daylight fixture)" % (name, scene))
        source = data.get("look_assist_wb_source")
        if source not in ALLOWED_WB_SOURCES:
            failures.append("%s: look_assist_wb_source=%r, want one of %s" % (name, source, ALLOWED_WB_SOURCES))
        temperature = data.get("look_assist_temperature")
        tint = data.get("look_assist_tint")
        if not isinstance(temperature, (int, float)) or not (DAYLIGHT_TEMPERATURE[0] <= temperature <= DAYLIGHT_TEMPERATURE[1]):
            failures.append("%s: look_assist_temperature=%r outside the daylight bounds %s" % (name, temperature, DAYLIGHT_TEMPERATURE))
        if not isinstance(tint, (int, float)) or not (DAYLIGHT_TINT[0] <= tint <= DAYLIGHT_TINT[1]):
            failures.append("%s: look_assist_tint=%r outside the daylight bounds %s" % (name, tint, DAYLIGHT_TINT))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--min-frames", type=int, default=1)
    args = parser.parse_args(argv)
    sidecars = load_sidecars(args.directory)
    if not sidecars:
        print("no saved frame-NN.json sidecars in %s" % args.directory)
        return 2
    failures = check(sidecars, args.min_frames)
    for failure in failures:
        print("FAIL " + failure)
    if not failures:
        print("PASS %d sidecar(s): scene=shade, wb_source=%s" % (len(sidecars), sidecars[0][1].get("look_assist_wb_source")))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
