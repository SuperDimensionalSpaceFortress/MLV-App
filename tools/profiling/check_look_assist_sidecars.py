#!/usr/bin/env python3
"""GUI-smoke assertion for Look Assist on the tracked DAYLIGHT fixture (stdlib only).

Reads the frame-NN.json sidecars a real-app contact-sheet capture writes (the app's contact-sheet-dir option) and asserts
what the classifier fix has to deliver *in the real app*, on the path the user sees:

  * settled == true and look_assist_wb_decision is an ACCEPTED one ("accepted", "accepted-damped" or
    "prior"): a sidecar captured before Look Assist settled, or after it rejected / fell back, says
    nothing about the shipped picture (a rejected-unstable decision used to pass this check), AND

  * look_assist_scene == "shade"  -- the daylight clip is not read as night, AND
  * look_assist_wb_source is "processed-neutral-patch" (white balance solved from a neutral patch of
    the RENDERED picture) or "as-shot-prior" (the clip's recorded balance, when no patch can be
    trusted) -- never "none". LOOK-ASSIST-SCENE-CLASSIFY-1 r1 had the right scene and source=none:
    no balance was solved at all and the picture went bluer (deck chroma 11.5 -> 22.4).

The decision half needs no numpy/Pillow, so it runs in the repo's pinned CI environment. The PICTURE
half is opt-in because it needs pixels: `--deck-chroma-max X` measures the CIELAB chroma of the
tracked pool deck (a physically near-neutral surface; rows 65-98 %, columns 2-25 % of frame-00) in
frame-00.png and fails above X. A correct decision with a wrong picture is a regression (r1: scene
right, deck chroma 11.5 -> 22.4); this is the GUI path's picture gate. Needs Pillow.

usage: check_look_assist_sidecars.py <contact-sheet-raw-dir> [--min-frames N] [--deck-chroma-max X]
exit 0 = pass, 1 = assertion failed, 2 = no usable sidecars (or Pillow missing for the picture gate).
"""
import argparse
import glob
import json
import os
import sys

ALLOWED_WB_SOURCES = ("processed-neutral-patch", "as-shot-prior")
ACCEPTED_WB_DECISIONS = ("accepted", "accepted-damped", "prior")
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
        if data.get("settled") is not True:
            failures.append("%s: settled=%r, want true (captured before Look Assist settled)" % (name, data.get("settled")))
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
    the real-app sheet metrics and the pipeline test). Pillow only; raises ImportError without it."""
    from PIL import Image  # noqa: WPS433 (deliberately lazy: the decision half must run without it)

    image = Image.open(png_path).convert("RGB")
    width, height = image.size
    x0, x1 = int(width * 0.02), int(width * 0.25)
    y0, y1 = int(height * 0.65), int(height * 0.98)
    pixels = list(image.crop((x0, y0, x1, y1)).getdata())
    if not pixels:
        return float("inf")
    mean = [sum(pixel[c] for pixel in pixels) / len(pixels) for c in range(3)]
    linear = []
    for value in mean:
        v = value / 255.0
        linear.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    x = (0.4124 * linear[0] + 0.3576 * linear[1] + 0.1805 * linear[2]) / 0.95047
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    z = (0.0193 * linear[0] + 0.1192 * linear[1] + 0.9505 * linear[2]) / 1.08883

    def f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else 7.787 * t + 16.0 / 116.0

    a = 500.0 * (f(x) - f(y))
    b = 200.0 * (f(y) - f(z))
    return (a * a + b * b) ** 0.5


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--min-frames", type=int, default=1)
    parser.add_argument("--deck-chroma-max", type=float, default=None,
                        help="also require the deck-region Lab chroma of frame-00.png to be <= this (needs Pillow)")
    args = parser.parse_args(argv)
    sidecars = load_sidecars(args.directory)
    if not sidecars:
        print("no saved frame-NN.json sidecars in %s" % args.directory)
        return 2
    failures = check(sidecars, args.min_frames)
    deck_note = ""
    if args.deck_chroma_max is not None:
        picture = os.path.join(args.directory, "frame-00.png")
        try:
            chroma = deck_cast_chroma(picture)
        except ImportError:
            print("Pillow is required for --deck-chroma-max")
            return 2
        except OSError as error:
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
