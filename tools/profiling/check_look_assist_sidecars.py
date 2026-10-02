#!/usr/bin/env python3
"""GUI-smoke assertion for Look Assist on the tracked DAYLIGHT fixture (stdlib only).

Reads the frame-NN.json sidecars a real-app contact-sheet capture writes (the app's contact-sheet-dir option) and asserts
what the classifier fix has to deliver *in the real app*, on the path the user sees:

  * settled == true and look_assist_wb_decision is an ACCEPTED one ("accepted" or "accepted-damped"): a
    sidecar captured before Look Assist settled, or after it rejected / fell back, says nothing about the
    shipped picture (a rejected-unstable decision used to pass this check), AND

  * look_assist_scene == "shade"  -- the daylight clip is not read as night, AND
  * look_assist_wb_source is "processed-neutral-patch" (white balance solved from a neutral patch of
    the RENDERED picture) or "rendered-neutral-patch" (the same patch solve, on a picture re-rendered at
    a white balance that has neutral samples) -- never "none" and never "as-shot-prior". LOOK-ASSIST-SCENE-CLASSIFY-1 r1 had the right scene and source=none: no balance
    was solved at all and the picture went bluer (deck chroma 11.5 -> 22.4); r2 labelled the same base
    balance "as-shot-prior" (deck chroma 18.9 in the real app against master's 13.5).

Both halves are stdlib-only (look_assist_png decodes the PNG), so they run in the repo's pinned CI environment.
The PICTURE half is opt-in because it needs pixels: `--deck-chroma-max X` measures the CIELAB chroma of the
tracked pool deck (a physically near-neutral surface; rows 65-98 %, columns 2-25 % of frame-00) in
frame-00.png and fails above X. A correct decision with a wrong picture is a regression (r1: scene
right, deck chroma 11.5 -> 22.4); this is the GUI path's picture gate.

PR #222 r2: a daylight verdict that nothing verifies falls back to MASTER's analysis (scene "night", source
"none": the picture master produces, never worse than it). That outcome is not this checker's pass -- it is the
improvement gate -- unless `--allow-master-fallback` says the caller only needs "no worse than master" (the
picture gate of check_look_assist_profile_states.py is what proves that); the half-states ("as-shot-prior",
"master-balance" under a shade scene) never pass.

usage: check_look_assist_sidecars.py <contact-sheet-raw-dir> [--min-frames N] [--deck-chroma-max X] [--allow-master-fallback]
exit 0 = pass, 1 = assertion failed, 2 = no usable sidecars (or an unreadable picture for the picture gate).
"""
import argparse
import glob
import json
import os
import sys

import look_assist_png

# "as-shot-prior" / "prior" are deliberately NOT accepted for the tracked daylight fixtures: the clip records
# no white balance beyond the app default (6000 K / tint 0), which is exactly the regression picture
# (deck chroma 18.9 in the real app) under a different label. A balance has to have been solved from, or
# refined on, the rendered picture.
ALLOWED_WB_SOURCES = ("processed-neutral-patch", "rendered-neutral-patch")
# Never acceptable, whatever the scene: the as-shot prior at the base balance (PR #221 r2) and a shade verdict
# carrying master's balance (PR #222 r1's no-surface state, which measured worse than master's look).
FORBIDDEN_WB_SOURCES = ("as-shot-prior", "prior", "master-balance")
ACCEPTED_WB_DECISIONS = ("accepted", "accepted-damped")
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


def is_master_analysis(scene, source):
    """The recorded verdict is master's own (the daylight verdict was not trusted and the clip was re-analysed as
    master analyses it): not daylight, and none of the half-states."""
    return scene != "shade" and source not in FORBIDDEN_WB_SOURCES


def check(sidecars, min_frames=1, allow_master_fallback=False):
    """Return a list of failure strings (empty = pass)."""
    failures = []
    if len(sidecars) < min_frames:
        failures.append("only %d saved sidecar(s), need %d" % (len(sidecars), min_frames))
    for name, data in sidecars:
        if not data.get("look_assist_enabled", False):
            failures.append("%s: look assist was not enabled" % name)
            continue
        if data.get("settled") is not True:
            failures.append("%s: settled=%r, want true (captured before Look Assist settled)" % (name, data.get("settled")))
        if allow_master_fallback and is_master_analysis(data.get("look_assist_scene"), data.get("look_assist_wb_source")):
            continue   # master's own look; whether it is no worse than master is the picture gate's call
        decision = data.get("look_assist_wb_decision")
        if decision not in ACCEPTED_WB_DECISIONS:
            failures.append("%s: look_assist_wb_decision=%r, want one of %s" % (name, decision, ACCEPTED_WB_DECISIONS))
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


def deck_cast_chroma(png_path):
    """CIELAB chroma of the mean colour of the pool-deck region of one frame (same region and maths as
    the real-app sheet metrics and the pipeline test). Stdlib only (look_assist_png): the picture half runs in
    the hosted CI image, which does not pin Pillow. ValueError for a PNG this reader does not support."""
    return look_assist_png.deck_cast_chroma(png_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--min-frames", type=int, default=1)
    parser.add_argument("--deck-chroma-max", type=float, default=None,
                        help="also require the deck-region Lab chroma of frame-00.png to be <= this")
    parser.add_argument("--allow-master-fallback", action="store_true",
                        help="accept master's own analysis (scene != shade, source none) as a pass")
    args = parser.parse_args(argv)
    sidecars = load_sidecars(args.directory)
    if not sidecars:
        print("no saved frame-NN.json sidecars in %s" % args.directory)
        return 2
    failures = check(sidecars, args.min_frames, args.allow_master_fallback)
    deck_note = ""
    if args.deck_chroma_max is not None:
        picture = os.path.join(args.directory, "frame-00.png")
        try:
            chroma = deck_cast_chroma(picture)
        except (OSError, ValueError) as error:
            print("cannot read %s: %s" % (picture, error))
            return 2
        deck_note = ", deck chroma %.1f <= %.1f" % (chroma, args.deck_chroma_max)
        if chroma > args.deck_chroma_max:
            failures.append("frame-00.png: deck chroma %.1f > %.1f (the picture is not neutral)" % (chroma, args.deck_chroma_max))
    for failure in failures:
        print("FAIL " + failure)
    if not failures:
        print("PASS %d sidecar(s): scene=shade, settled, wb_source=%s%s"
              % (len(sidecars), sidecars[0][1].get("look_assist_wb_source"), deck_note))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
