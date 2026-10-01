#!/usr/bin/env python3
"""Objective floor for Look Assist frames: the part of 'does it look right' a number CAN settle.

WHY THIS EXISTS (LOOK-METRICS-JUDGE-1, DESIGN.md AMENDMENT 1 A3 + AMENDMENT 2 B3)
    Owner ruling 2026-09-30: models, not the owner, judge aesthetics. Bus evidence (TRAPS s8) says a model
    judging a blinded image pair can sit near chance, so the numbers here GATE and the judges
    (look_judges / look_tally) only REFINE. A frame that fails this floor is not rescued by a judge.

WHAT IT MEASURES (all on the ACTIVE area -- see letterbox below)
    per frame   clipped-highlight %, crushed-shadow % (both reuse make-contact-sheet.py channel_stats, so a
                number here is comparable with every existing stats sidecar), a saturation ceiling (mean HSV
                saturation + share of oversaturated pixels), skin-tone-region hue drift against a baseline
                frame where a skin-tone region is detectable.
    per pair    CUDA-vs-CPU per-channel mean / max absolute delta, per-channel signed bias, mismatch fraction,
                luma SSIM (implemented here in numpy: no scipy / scikit-image on the tooling hosts).
    scope       SCOPE=shader-subset is GATED; SCOPE=full-look is REPORTED, never gated (A3).

THE RULES IT ENCODES
    * Typed terminals, zero partial credit. A frame is PASS | FAIL | NOT_EVALUABLE; a sheet is PASS only if
      every frame is PASS. An empty sheet or one unreadable frame is INCOMPLETE, never PASS.
    * N/A is never a pass. With no baseline frame, or a baseline with no skin-tone region, the skin check is
      NOT_APPLICABLE and the sheet verdict says how many frames it actually applied to. A baseline that HAS a
      skin region while the subject has lost it (or kept less than skin_region_retain_fraction of it) is a FAIL:
      a look that pushes skin out of the colour box is the worst drift, never "not applicable".
    * Nothing is hidden from the floor unless the caller says so. A playback capture can include black window
      bars (the Ultra-Magnus CUDA fixture tiles are 16:9 with ~25% black bars; the CPU tiles are content-only),
      but pixels alone cannot tell a bar from a crushed region of the scene. Bars are excluded only when the
      caller DECLARES them (--letterbox-bars) or opts in to SYMMETRIC auto-detection (--letterbox auto-symmetric,
      with a stated tolerance); a one-sided dark band is always scene content. Undeclared, the full frame is
      measured. Whichever applies is written into every frame verdict (mode, provenance, candidate bands).
    * Thresholds live in ONE tracked file with a reason per value (look_floor_config.json); a verdict carries
      the config's sha256, its version and any override that was applied.

Requires Pillow + numpy (pinned, with hashes, in .github/requirements/repo-hygiene.txt, so the pixel tests run
in hosted CI). Pure-stdlib pieces live in look_config.py / look_tally.py so the judging path needs neither.
"""
import importlib.util
import math
import os
import re

import numpy as np
from PIL import Image

import look_config

SCHEMA_FRAME_SHEET = "mlv-app/look-floor-verdict/v1"
SCHEMA_PAIR = "mlv-app/look-pair-metrics-verdict/v1"

PASS = "PASS"
FAIL = "FAIL"
NOT_EVALUABLE = "NOT_EVALUABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
INCOMPLETE = "INCOMPLETE"
REPORTED = "REPORTED"
GEOMETRY_MISMATCH = "GEOMETRY_MISMATCH"

_CONTACT_SHEET_MODULE = None


def _contact_sheet():
    """make-contact-sheet.py has a hyphen, so it cannot be imported by name (test_make_contact_sheet.py does the same)."""
    global _CONTACT_SHEET_MODULE
    if _CONTACT_SHEET_MODULE is None:
        path = os.path.join(look_config.HERE, os.pardir, "make-contact-sheet.py")
        spec = importlib.util.spec_from_file_location("make_contact_sheet_reused", os.path.normpath(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTACT_SHEET_MODULE = module
    return _CONTACT_SHEET_MODULE


# ---------------------------------------------------------------------------------------------------------
# loading + letterbox
# ---------------------------------------------------------------------------------------------------------

def load_rgb(source):
    """uint8 HxWx3 from a path, a PIL image or an array. Alpha is dropped; anything else raises ValueError."""
    if isinstance(source, np.ndarray):
        arr = source
    elif isinstance(source, Image.Image):
        arr = np.asarray(source.convert("RGB"))
    else:
        with Image.open(source) as image:
            arr = np.asarray(image.convert("RGB"))
    if arr.ndim != 3 or arr.shape[2] < 3 or arr.dtype != np.uint8:
        raise ValueError(f"expected a uint8 HxWx3 image, got shape={arr.shape} dtype={arr.dtype}")
    return np.ascontiguousarray(arr[:, :, :3])


LETTERBOX_MODES = ("off", "auto-symmetric")
_SIDES = ("top", "bottom", "left", "right")


def make_letterbox_policy(cfg, mode=None, declared=None):
    """The caller's decision about what the floor may hide. mode defaults from the config (OFF as shipped);
    `declared` is an explicit {top,bottom,left,right} (px, missing sides 0) naming bars the caller KNOWS exist."""
    policy = {"mode": "auto-symmetric" if cfg["letterbox"]["auto_exclude_symmetric"] else "off", "declared": None}
    if mode is not None:
        if mode not in LETTERBOX_MODES:
            raise ValueError(f"letterbox mode must be one of {LETTERBOX_MODES}, got {mode!r}")
        policy["mode"] = mode
    if declared is not None:
        clean = {}
        for side in _SIDES:
            value = declared.get(side, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"declared letterbox {side} must be a non-negative integer, got {value!r}")
            clean[side] = value
        extra = set(declared) - set(_SIDES)
        if extra:
            raise ValueError(f"declared letterbox has unknown sides {sorted(extra)}")
        policy["declared"] = clean
    return policy


def parse_declared_bars(spec):
    """'top=34,bottom=34' -> {'top': 34, 'bottom': 34}. Anything else is refused."""
    declared = {}
    for part in (spec or "").split(","):
        name, _, value = part.strip().partition("=")
        if name not in _SIDES or not value.strip().isdigit():
            raise ValueError(f"--letterbox-bars wants top=N,bottom=N,left=N,right=N; got {spec!r}")
        declared[name] = int(value)
    if not declared:
        raise ValueError("--letterbox-bars names no side")
    return declared


def _leading_run(flags):
    return int(np.argmin(flags)) if not flags.all() else len(flags)


def _decide_axis(cand_lo, cand_hi, declared_pair, mode, tolerance):
    """(lo, hi, provenance, note) for one axis. Only a DECLARED pair, or a SYMMETRIC pair under the auto opt-in,
    is ever applied; everything else is left in the measured area."""
    if declared_pair is not None:
        lo, hi = declared_pair
        if lo > cand_lo or hi > cand_hi:
            return 0, 0, "NONE", "DECLARED_BARS_NOT_DARK"
        return lo, hi, ("DECLARED" if (lo or hi) else "NONE"), None
    if not (cand_lo or cand_hi):
        return 0, 0, "NONE", None
    if mode != "auto-symmetric":
        return 0, 0, "NONE", "UNDECLARED_DARK_BANDS_MEASURED_AS_SCENE"
    if cand_lo > 0 and cand_hi > 0 and abs(cand_lo - cand_hi) <= tolerance:
        return cand_lo, cand_hi, "AUTO_SYMMETRIC", None
    return 0, 0, "NONE", "ONE_SIDED_DARK_BAND_IS_SCENE_CONTENT"


def detect_letterbox(arr, cfg, policy=None):
    """Find dark bands touching the frame edges and decide which ones may be excluded. Returns a dict whose
    top/bottom/left/right are the crop sizes APPLIED (all zero unless the policy allowed an exclusion);
    'candidate' always lists the dark bands that were SEEN, applied or not, so nothing is hidden silently."""
    lb = cfg["letterbox"]
    policy = policy or make_letterbox_policy(cfg)
    height, width = arr.shape[:2]
    result = {
        "mode": policy["mode"], "provenance": "NONE", "detected": False, "top": 0, "bottom": 0, "left": 0,
        "right": 0, "excludedPct": 0.0, "refused": None, "notes": [],
        "candidate": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        "declared": policy.get("declared"), "symmetryTolerancePx": int(lb["symmetry_tolerance_px"]),
    }
    bar_max = int(lb["bar_max_code"])
    row_is_bar = arr.max(axis=(1, 2)) <= bar_max
    if row_is_bar.all():
        result["refused"] = "ALL_BLACK_FRAME"
        return result
    declared = policy.get("declared")
    tol = int(lb["symmetry_tolerance_px"])
    top_c, bottom_c = _leading_run(row_is_bar), _leading_run(row_is_bar[::-1])
    top, bottom, prov_v, note_v = _decide_axis(
        top_c, bottom_c, (declared["top"], declared["bottom"]) if declared else None, policy["mode"], tol)
    rows = arr[top:height - bottom]
    col_is_bar = rows.max(axis=(0, 2)) <= bar_max
    left_c, right_c = _leading_run(col_is_bar), _leading_run(col_is_bar[::-1])
    left, right, prov_h, note_h = _decide_axis(
        left_c, right_c, (declared["left"], declared["right"]) if declared else None, policy["mode"], tol)
    result["candidate"] = {"top": top_c, "bottom": bottom_c, "left": left_c, "right": right_c}
    result["notes"] = [n for n in (note_v, note_h) if n]
    if "DECLARED_BARS_NOT_DARK" in result["notes"]:
        result["refused"] = "DECLARED_BARS_NOT_DARK"
        return result
    active_h = height - top - bottom
    active_w = width - left - right
    if min(active_h, active_w) < int(lb["min_active_px"]):
        result["refused"] = "ACTIVE_AREA_TOO_SMALL"
        return result
    excluded = 100.0 * (1.0 - (active_h * active_w) / float(height * width))
    if excluded > float(lb["max_excluded_pct"]):
        result["refused"] = "EXCLUSION_IMPLAUSIBLE_PROBABLY_DARK_FRAME"
        result["excludedPct"] = round(excluded, 4)
        return result
    applied = bool(top or bottom or left or right)
    provenance = "DECLARED" if "DECLARED" in (prov_v, prov_h) else ("AUTO_SYMMETRIC" if applied else "NONE")
    result.update(
        {"detected": applied, "provenance": provenance, "top": top, "bottom": bottom, "left": left,
         "right": right, "excludedPct": round(excluded, 4)}
    )
    return result


def crop_active(arr, letterbox):
    height, width = arr.shape[:2]
    return arr[letterbox["top"]:height - letterbox["bottom"], letterbox["left"]:width - letterbox["right"]]


# ---------------------------------------------------------------------------------------------------------
# colour helpers
# ---------------------------------------------------------------------------------------------------------

def rgb_to_hsv(arr):
    """Return (hue_deg, saturation 0..1, value 0..1) float64 arrays for a uint8 RGB array."""
    f = arr.astype(np.float64) / 255.0
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(
            mx == r, ((g - b) / d) % 6.0, np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0)
        ) * 60.0
        s = np.where(mx > 0, d / mx, 0.0)
    h = np.where(d > 0, h, 0.0)
    return h, s, mx


def luma(arr):
    f = arr.astype(np.float64)
    return 0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]


def skin_tone_region(arr, cfg):
    """Boolean mask of skin-tone-coloured pixels. This is a COLOUR region, not a face detector: sand and wood
    can land in it. That is why the check is a drift against a baseline of the same scene, never an absolute."""
    skin = cfg["skin"]
    f = arr.astype(np.float64)
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    hue, sat, _ = rgb_to_hsv(arr)
    return (
        (cb >= skin["cb_range"][0]) & (cb <= skin["cb_range"][1])
        & (cr >= skin["cr_range"][0]) & (cr <= skin["cr_range"][1])
        & (y >= skin["min_luma"])
        & (hue >= skin["hue_range_deg"][0]) & (hue <= skin["hue_range_deg"][1])
        & (sat >= skin["sat_range"][0]) & (sat <= skin["sat_range"][1])
    )


def circular_mean_deg(hues_deg):
    rad = np.deg2rad(hues_deg)
    return float(np.rad2deg(math.atan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360.0)


def hue_distance_deg(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def skin_summary(active, cfg):
    mask = skin_tone_region(active, cfg)
    pct = 100.0 * float(mask.mean()) if mask.size else 0.0
    usable = pct >= float(cfg["frame"]["skin_min_pixel_pct"])
    hue_deg = None
    if usable:
        hue, _, _ = rgb_to_hsv(active)
        hue_deg = round(circular_mean_deg(hue[mask]), 4)
    return {"regionPct": round(pct, 4), "usable": usable, "meanHueDeg": hue_deg, "mask": mask}


def _centre_align(a, b, tolerance_px):
    """Centre-crop two H x W x 3 arrays to their common size when each axis differs by <= tolerance_px; None otherwise."""
    dh, dw = abs(a.shape[0] - b.shape[0]), abs(a.shape[1] - b.shape[1])
    if dh > tolerance_px or dw > tolerance_px:
        return None
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])

    def centre(x):
        t, left = (x.shape[0] - h) // 2, (x.shape[1] - w) // 2
        return x[t:t + h, left:left + w]

    return centre(a), centre(b)


def skin_drift_check(active, ref_active, cfg):
    """Skin-hue drift of `active` against a baseline that is known to HAVE a usable skin-tone region.

    The subject cannot dodge the check by losing the region: a subject whose region is empty, below the minimum
    share, or smaller than skin_region_retain_fraction of the baseline's is a FAIL. Drift is measured over the
    BASELINE's mask (the same pixels in both frames) whenever the two active areas align after a small centre
    crop, so a look that moves only some of the skin out of the colour box still shows as drift."""
    frame_cfg = cfg["frame"]
    threshold = frame_cfg["skin_hue_drift_deg_max"]
    ref_skin = skin_summary(ref_active, cfg)
    skin = skin_summary(active, cfg)
    detail = {"baselineRegionPct": ref_skin["regionPct"], "subjectRegionPct": skin["regionPct"],
              "retainFraction": float(frame_cfg["skin_region_retain_fraction"]), "threshold": threshold,
              "comparator": "<="}
    if not ref_skin["usable"] or not bool(ref_skin["mask"].any()):
        detail.update({"outcome": NOT_APPLICABLE, "reason": "NO_SKIN_TONE_REGION_IN_BASELINE"})
        return detail, skin
    retained = skin["regionPct"] >= detail["retainFraction"] * ref_skin["regionPct"]
    if not retained or skin["regionPct"] <= 0.0:
        detail.update({"outcome": FAIL, "reason": "SKIN_REGION_LOST_OR_SHRUNK", "value": None})
        return detail, skin
    aligned = _centre_align(active, ref_active, int(frame_cfg["skin_baseline_crop_tolerance_px"]))
    if aligned is not None:
        sub, ref = aligned
        mask = skin_tone_region(ref, cfg)
        hue_sub, _, _ = rgb_to_hsv(sub)
        hue_ref, _, _ = rgb_to_hsv(ref)
        sub_mean, ref_mean = circular_mean_deg(hue_sub[mask]), circular_mean_deg(hue_ref[mask])
        basis = "BASELINE_MASK"
    else:
        # Two frames that cannot be laid over each other have no common set of pixels, and comparing each frame's
        # OWN region would let a partial skin move through. Say so instead of guessing.
        detail.update({"outcome": NOT_EVALUABLE, "reason": "SKIN_MASKS_NOT_ALIGNABLE", "value": None,
                       "subjectActive": [int(active.shape[1]), int(active.shape[0])],
                       "baselineActive": [int(ref_active.shape[1]), int(ref_active.shape[0])]})
        return detail, skin
    check = _check(hue_distance_deg(sub_mean, ref_mean), threshold, "<=")
    detail.update(check)
    detail.update({"baselineMeanHueDeg": round(ref_mean, 4), "subjectMeanHueDeg": round(sub_mean, 4),
                   "maskBasis": basis})
    return detail, skin


# ---------------------------------------------------------------------------------------------------------
# per-frame floor
# ---------------------------------------------------------------------------------------------------------

def _check(value, threshold, comparator):
    ok = value <= threshold if comparator == "<=" else value >= threshold
    return {"value": round(float(value), 6), "threshold": threshold, "comparator": comparator,
            "outcome": PASS if ok else FAIL}


def evaluate_frame(source, cfg, reference=None, frame_id=None, letterbox_policy=None, baseline_requested=False):
    """Floor verdict for ONE frame. `reference` (optional) is the baseline frame for the skin-hue drift.
    `baseline_requested` says the caller ASKED for a baseline comparison: with no `reference` the skin check is then
    NOT_EVALUABLE (BASELINE_REQUESTED_BUT_NO_FRAME_FOR_INDEX), never the benign NOT_APPLICABLE of "nobody asked".
    `letterbox_policy` (make_letterbox_policy) says what the caller allows the floor to exclude; default OFF."""
    baseline_requested = bool(baseline_requested or reference is not None)
    verdict = {"frameId": frame_id, "outcome": NOT_EVALUABLE, "checks": {}, "metrics": {}, "geometry": {}}
    try:
        arr = load_rgb(source)
    except Exception as exc:  # unreadable / wrong type: a typed terminal, never a silent skip
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        return verdict
    ref_arr = None
    if reference is not None:
        try:
            ref_arr = load_rgb(reference)
        except Exception as exc:  # a baseline that was GIVEN but cannot be read is not "no baseline"
            verdict["error"] = f"BASELINE_UNREADABLE: {type(exc).__name__}: {exc}"
            return verdict
    policy = letterbox_policy or make_letterbox_policy(cfg)
    letterbox = detect_letterbox(arr, cfg, policy)
    active = crop_active(arr, letterbox)
    verdict["geometry"] = {
        "width": int(arr.shape[1]), "height": int(arr.shape[0]),
        "activeWidth": int(active.shape[1]), "activeHeight": int(active.shape[0]), "letterbox": letterbox,
    }
    if active.size == 0:
        verdict["error"] = "EMPTY_ACTIVE_AREA"
        return verdict

    stats = _contact_sheet().channel_stats(Image.fromarray(active, "RGB"))  # reuse: same luma/clip/crush definition
    _, sat, val = rgb_to_hsv(active)
    frame_cfg = cfg["frame"]
    oversat = (sat >= frame_cfg["oversaturated_sat_min"]) & (val >= frame_cfg["oversaturated_value_min"])
    metrics = {
        "luma_p1": stats["luma_p1"], "luma_p50": stats["luma_p50"], "luma_p99": stats["luma_p99"],
        "clipped_highlight_pct": stats["clipped_highlight_pct"],
        "crushed_shadow_pct": stats["crushed_black_pct"],
        "mean_saturation": stats["mean_saturation"],
        "oversaturated_pixel_pct": 100.0 * float(oversat.mean()),
    }
    checks = {
        "clipped_highlight": _check(metrics["clipped_highlight_pct"], frame_cfg["clipped_highlight_pct_max"], "<="),
        "crushed_shadow": _check(metrics["crushed_shadow_pct"], frame_cfg["crushed_shadow_pct_max"], "<="),
        "mean_saturation": _check(metrics["mean_saturation"], frame_cfg["mean_saturation_max"], "<="),
        "oversaturated_pixels": _check(
            metrics["oversaturated_pixel_pct"], frame_cfg["oversaturated_pixel_pct_max"], "<="),
    }

    skin = skin_summary(active, cfg)
    metrics["skin_tone_region_pct"] = skin["regionPct"]
    metrics["skin_tone_mean_hue_deg"] = skin["meanHueDeg"]
    metrics["crushed_shadow_pct_full_frame"] = _contact_sheet().channel_stats(
        Image.fromarray(arr, "RGB"))["crushed_black_pct"]
    if ref_arr is None and baseline_requested:
        skin_check = {"outcome": NOT_EVALUABLE, "threshold": frame_cfg["skin_hue_drift_deg_max"],
                      "comparator": "<=", "reason": "BASELINE_REQUESTED_BUT_NO_FRAME_FOR_INDEX"}
    elif ref_arr is None:
        skin_check = {"outcome": NOT_APPLICABLE, "threshold": frame_cfg["skin_hue_drift_deg_max"],
                      "comparator": "<=", "reason": "NO_BASELINE_FRAME"}
    else:
        ref_policy = {"mode": policy["mode"], "declared": None}  # a declaration describes the SUBJECT's capture
        ref_active = crop_active(ref_arr, detect_letterbox(ref_arr, cfg, ref_policy))
        skin_check, _ = skin_drift_check(active, ref_active, cfg)
    skin_check["baselineRequested"] = baseline_requested
    checks["skin_hue_drift"] = skin_check

    verdict["metrics"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}
    verdict["checks"] = checks
    failed = sorted(name for name, c in checks.items() if c["outcome"] == FAIL)
    unevaluable = sorted(name for name, c in checks.items() if c["outcome"] == NOT_EVALUABLE)
    verdict["failedChecks"] = failed
    verdict["unevaluableChecks"] = unevaluable
    # A concrete FAIL stands; otherwise a check that could not be run keeps the frame from reading as a pass.
    verdict["outcome"] = FAIL if failed else (NOT_EVALUABLE if unevaluable else PASS)
    return verdict


class FrameIndexError(ValueError):
    """A supplied frames directory that cannot be used at all: missing, not a directory, or yielding no frame."""


# An image file that could have been a frame. Skipping one is never silent AND never harmless: the sheet is INCOMPLETE.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".exr", ".dng", ".ppm", ".pgm")


class FrameIndex(dict):
    """{index: path} that also remembers every entry of the directory it did NOT index (`skipped`), so a
    mis-named or non-PNG frame can never just vanish from a sheet. A skipped entry is blocking when it could
    have been a frame (an image file, or a second file claiming an index already taken); a sidecar such as a
    notes file is listed but does not block."""

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.skipped = []

    def _skip(self, name, reason, blocking):
        self.skipped.append({"name": name, "reason": reason, "blocking": blocking})

    def blocking_files(self):
        return [s["name"] for s in self.skipped if s["blocking"]]

    def summary(self):
        return {"directory": self.directory, "indexedFrames": len(self), "indices": sorted(self),
                "skipped": [dict(s) for s in self.skipped]}


def index_frames(directory):
    """FrameIndex ({index: path}) for the PNGs of a frames dir; the index is the LAST run of digits in the stem.
    Nothing in the directory is skipped silently (see FrameIndex.skipped), and a directory that is missing or
    yields zero frames raises FrameIndexError."""
    if not os.path.isdir(directory):
        raise FrameIndexError(f"frames directory {directory!r} does not exist or is not a directory")
    found = FrameIndex(directory)
    owner = {}
    for name in sorted(os.listdir(directory)):
        if not os.path.isfile(os.path.join(directory, name)):
            found._skip(name, "NOT_A_FILE", False)
            continue
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext != ".png":
            is_image = ext in _IMAGE_EXTENSIONS
            found._skip(name, "NOT_A_PNG" if is_image else "NOT_A_FRAME_FILE", is_image)
            continue
        digits = re.findall(r"\d+", stem)
        if not digits:
            found._skip(name, "NO_DIGITS_IN_NAME", True)
            continue
        index = int(digits[-1])
        if index in owner:
            found._skip(name, f"DUPLICATE_FRAME_INDEX: index {index} is already taken by {owner[index]}", True)
            continue
        owner[index] = name
        found[index] = os.path.join(directory, name)
    if not found:
        raise FrameIndexError(
            f"frames directory {directory!r} yielded no frame (skipped: "
            f"{[s['name'] + ' ' + s['reason'] for s in found.skipped] or 'directory is empty'})")
    return found


def _file_reasons(index, code):
    """incompleteReasons entries for the blocking skips of a FrameIndex (plain dicts carry none)."""
    files = index.blocking_files() if isinstance(index, FrameIndex) else []
    return [{"code": code, "files": files}] if files else []


def _summary(index):
    return index.summary() if isinstance(index, FrameIndex) else None


def _config_block(meta, overrides):
    return {"configSha256": meta["configSha256"], "configVersion": meta.get("configVersion"),
            "overrides": overrides or {}}


def _skin_status(requested, verdicts, applied):
    """One word for what the skin check amounted to on this sheet, so 'requested but not applicable' is never
    mistaken for 'checked and fine'."""
    if not requested:
        return "NOT_REQUESTED"
    checks = [v["checks"].get("skin_hue_drift", {}) for v in verdicts]
    if any(c.get("outcome") == NOT_EVALUABLE for c in checks) or any(v["outcome"] == NOT_EVALUABLE for v in verdicts):
        return "INCOMPLETE"
    if applied == 0:
        return "REQUESTED_NOT_APPLICABLE"
    return "APPLIED" if applied == len(verdicts) else "APPLIED_PARTIAL"


def evaluate_sheet(frames, cfg, meta, label="", references=None, overrides=None, letterbox_policy=None,
                   baseline_requested=False):
    """Sheet verdict. `frames` = {index: path-or-array}; `references` = {index: baseline} for the skin drift.

    `baseline_requested` (also implied by a non-empty `references`) says the caller ASKED for a baseline comparison.
    Then a subject frame without a baseline frame, a baseline frame without a subject frame, and any image file
    the frame indexer could not place are all INCOMPLETE with a typed entry in `incompleteReasons`: a requested
    input that is missing never reads as agreement."""
    requested = bool(baseline_requested or references)
    ref_index = references
    references = references or {}
    policy = letterbox_policy or make_letterbox_policy(cfg)
    frame_verdicts = []
    for index in sorted(frames):
        v = evaluate_frame(frames[index], cfg, reference=references.get(index), frame_id=index,
                           letterbox_policy=policy, baseline_requested=requested)
        if isinstance(frames[index], str) and os.path.isfile(frames[index]):
            v["imageSha256"] = look_config.sha256_file(frames[index])
        frame_verdicts.append(v)
    counts = {PASS: 0, FAIL: 0, NOT_EVALUABLE: 0}
    for v in frame_verdicts:
        counts[v["outcome"]] += 1
    skin_checks = {v["frameId"]: v["checks"].get("skin_hue_drift", {}) for v in frame_verdicts}
    skin_applicable = sum(1 for c in skin_checks.values() if c.get("outcome") in (PASS, FAIL))
    missing = sorted(i for i in frames if requested and i not in references)
    extra = sorted(i for i in references if requested and i not in frames)
    reasons = _file_reasons(frames, "FRAME_FILES_NOT_INDEXED") + _file_reasons(ref_index, "BASELINE_FILES_NOT_INDEXED")
    if not frame_verdicts:
        reasons.append({"code": "NO_FRAMES"})
    if missing:
        reasons.append({"code": "BASELINE_MISSING_FRAMES", "indices": missing})
    if extra:
        reasons.append({"code": "BASELINE_FRAMES_WITHOUT_SUBJECT", "indices": extra})
    not_evaluable = [v["frameId"] for v in frame_verdicts if v["outcome"] == NOT_EVALUABLE and v["frameId"] not in missing]
    if not_evaluable:
        reasons.append({"code": "FRAME_NOT_EVALUABLE", "indices": not_evaluable})
    outcome = INCOMPLETE if reasons else (FAIL if counts[FAIL] else PASS)
    undeclared = [v["frameId"] for v in frame_verdicts
                  if v.get("geometry", {}).get("letterbox", {}).get("notes")]
    return {
        "schema": SCHEMA_FRAME_SHEET, "kind": "floor-sheet", "subject": label, "outcome": outcome,
        "incompleteReasons": reasons,
        "frameCount": len(frame_verdicts), "counts": counts,
        "skinCheck": {
            "status": _skin_status(requested, frame_verdicts, skin_applicable),
            "appliedToFrames": skin_applicable, "ofFrames": len(frame_verdicts),
            "notApplicableFrames": {str(i): c.get("reason") for i, c in skin_checks.items()
                                    if c.get("outcome") == NOT_APPLICABLE},
        },
        "baseline": {"requested": requested, "framesMissingBaseline": missing,
                     "baselineIndicesWithoutSubjectFrame": extra},
        "inputs": {"frames": _summary(frames), "baseline": _summary(ref_index)},
        "letterboxPolicy": policy, "framesWithDarkBandsMeasuredAsScene": undeclared,
        "config": _config_block(meta, overrides), "frames": frame_verdicts,
    }


# ---------------------------------------------------------------------------------------------------------
# SSIM (Wang et al. 2004): Gaussian 11x11, sigma 1.5, K1=0.01, K2=0.03, mean over the valid region
# ---------------------------------------------------------------------------------------------------------

def _gaussian_kernel(win, sigma):
    ax = np.arange(win, dtype=np.float64) - (win - 1) / 2.0
    k = np.exp(-(ax ** 2) / (2.0 * sigma * sigma))
    return k / k.sum()


def _filter_valid(x, k):
    """Separable 'valid' correlation with 1-D kernel k, as a sum of shifted slices (no big window tensor)."""
    win = len(k)
    out_w = x.shape[1] - win + 1
    tmp = np.zeros((x.shape[0], out_w), dtype=np.float64)
    for i in range(win):
        tmp += k[i] * x[:, i:i + out_w]
    out_h = x.shape[0] - win + 1
    out = np.zeros((out_h, out_w), dtype=np.float64)
    for i in range(win):
        out += k[i] * tmp[i:i + out_h, :]
    return out


def ssim(a, b, data_range=255.0, win=11, sigma=1.5, k1=0.01, k2=0.03):
    """Mean SSIM of two 2-D float arrays of identical shape. Raises ValueError when the image is smaller than
    the window (a number from a clipped window would not be SSIM)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 2:
        raise ValueError(f"ssim needs two 2-D arrays of one shape, got {a.shape} vs {b.shape}")
    if min(a.shape) < win:
        raise ValueError(f"image {a.shape} is smaller than the {win}x{win} SSIM window")
    kernel = _gaussian_kernel(win, sigma)
    mu_a = _filter_valid(a, kernel)
    mu_b = _filter_valid(b, kernel)
    var_a = _filter_valid(a * a, kernel) - mu_a * mu_a
    var_b = _filter_valid(b * b, kernel) - mu_b * mu_b
    cov = _filter_valid(a * b, kernel) - mu_a * mu_b
    c1 = (k1 * data_range) ** 2
    c2 = (k2 * data_range) ** 2
    num = (2.0 * mu_a * mu_b + c1) * (2.0 * cov + c2)
    den = (mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2)
    return float((num / den).mean())


# ---------------------------------------------------------------------------------------------------------
# CUDA-vs-CPU pair metrics
# ---------------------------------------------------------------------------------------------------------

def pair_metrics(a_rgb, b_rgb, mismatch_tol):
    """Numbers only. a/b are uint8 HxWx3 of identical shape. Deltas are B minus A (so 'a' is the reference)."""
    diff = b_rgb.astype(np.int16) - a_rgb.astype(np.int16)
    absd = np.abs(diff)
    return {
        "meanAbsDeltaPerChannel": [round(float(absd[:, :, c].mean()), 6) for c in range(3)],
        "maxAbsDeltaPerChannel": [int(absd[:, :, c].max()) for c in range(3)],
        "meanSignedDeltaPerChannel": [round(float(diff[:, :, c].mean()), 6) for c in range(3)],
        "mismatchFraction": round(float((absd.max(axis=2) > mismatch_tol).mean()), 6),
        "ssimLuma": round(ssim(luma(a_rgb), luma(b_rgb)), 6),
    }


def compare_frames(a_src, b_src, cfg, scope, frame_id=None, letterbox_policy_a=None, letterbox_policy_b=None):
    """Pair verdict for one frame index. a = reference backend, b = subject backend (named by the caller).
    Each side has its own letterbox policy (a declaration describes ONE capture); default OFF."""
    if scope not in cfg["pair"]["scopes"]:
        raise ValueError(f"unknown scope {scope!r}; known: {sorted(cfg['pair']['scopes'])}")
    scope_cfg = cfg["pair"]["scopes"][scope]
    out = {"frameId": frame_id, "scope": scope, "gated": bool(scope_cfg["gated"]), "outcome": INCOMPLETE}
    try:
        a_full, b_full = load_rgb(a_src), load_rgb(b_src)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    lb_a = detect_letterbox(a_full, cfg, letterbox_policy_a)
    lb_b = detect_letterbox(b_full, cfg, letterbox_policy_b)
    a, b = crop_active(a_full, lb_a), crop_active(b_full, lb_b)
    out["geometry"] = {"aActive": [int(a.shape[1]), int(a.shape[0])], "bActive": [int(b.shape[1]), int(b.shape[0])],
                       "aLetterbox": lb_a, "bLetterbox": lb_b}
    tol = int(cfg["pair"]["geometry_tolerance_px"])
    dh, dw = abs(a.shape[0] - b.shape[0]), abs(a.shape[1] - b.shape[1])
    if dh > tol or dw > tol:
        out["outcome"] = GEOMETRY_MISMATCH
        return out
    if dh or dw:  # within tolerance: centre-crop both to the common size, and say so
        h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])

        def centre(x):
            t, l = (x.shape[0] - h) // 2, (x.shape[1] - w) // 2
            return x[t:t + h, l:l + w]

        a, b = centre(a), centre(b)
        out["geometry"]["centreCroppedTo"] = [int(w), int(h)]
    try:
        m = pair_metrics(a, b, int(cfg["pair"]["mismatch_channel_tolerance"]))
    except ValueError as exc:
        out["error"] = str(exc)
        return out
    out["metrics"] = m
    checks = {
        "mean_abs_delta": _check(max(m["meanAbsDeltaPerChannel"]), scope_cfg["mean_abs_delta_max"], "<="),
        "mismatch_fraction": _check(m["mismatchFraction"], scope_cfg["mismatch_fraction_max"], "<="),
        "ssim_luma": _check(m["ssimLuma"], scope_cfg["ssim_min"], ">="),
    }
    if scope_cfg["max_abs_delta_max"] is not None:
        checks["max_abs_delta"] = _check(max(m["maxAbsDeltaPerChannel"]), scope_cfg["max_abs_delta_max"], "<=")
    out["checks"] = checks
    met = all(c["outcome"] == PASS for c in checks.values())
    out["thresholdsMet"] = met
    out["outcome"] = (PASS if met else FAIL) if scope_cfg["gated"] else REPORTED
    return out


def compare_sheets(a_frames, b_frames, cfg, meta, scope, a_label="a", b_label="b", overrides=None,
                   letterbox_policy_a=None, letterbox_policy_b=None):
    """Pair two frame sets by index. Any unpaired index, or any frame that could not be compared, makes the
    sheet INCOMPLETE -- a missing side is never read as agreement (DESIGN.md P2)."""
    indices = sorted(set(a_frames) | set(b_frames))
    unpaired = [i for i in indices if i not in a_frames or i not in b_frames]
    verdicts = [compare_frames(a_frames[i], b_frames[i], cfg, scope, frame_id=i,
                               letterbox_policy_a=letterbox_policy_a, letterbox_policy_b=letterbox_policy_b)
                for i in indices if i not in unpaired]
    gated = bool(cfg["pair"]["scopes"][scope]["gated"])
    broken = [v for v in verdicts if v["outcome"] in (INCOMPLETE, GEOMETRY_MISMATCH)]
    reasons = _file_reasons(a_frames, "A_FILES_NOT_INDEXED") + _file_reasons(b_frames, "B_FILES_NOT_INDEXED")
    if not verdicts:
        reasons.append({"code": "NO_FRAMES"})
    if unpaired:
        reasons.append({"code": "UNPAIRED_INDICES", "indices": unpaired})
    if broken:
        reasons.append({"code": "FRAME_NOT_COMPARABLE", "indices": [v["frameId"] for v in broken]})
    if reasons:
        outcome = INCOMPLETE
    elif not gated:
        outcome = REPORTED
    else:
        outcome = FAIL if any(v["outcome"] == FAIL for v in verdicts) else PASS
    agg = None
    if verdicts and not broken:
        ms = [v["metrics"] for v in verdicts if "metrics" in v]
        if ms:
            agg = {
                "meanAbsDeltaPerChannel": [round(float(np.mean([m["meanAbsDeltaPerChannel"][c] for m in ms])), 6) for c in range(3)],
                "maxAbsDeltaPerChannel": [max(m["maxAbsDeltaPerChannel"][c] for m in ms) for c in range(3)],
                "meanMismatchFraction": round(float(np.mean([m["mismatchFraction"] for m in ms])), 6),
                "meanSsimLuma": round(float(np.mean([m["ssimLuma"] for m in ms])), 6),
                "minSsimLuma": round(float(np.min([m["ssimLuma"] for m in ms])), 6),
            }
    return {
        "schema": SCHEMA_PAIR, "kind": "pair-sheet", "scope": scope, "gated": gated, "outcome": outcome,
        "incompleteReasons": reasons, "inputs": {"a": _summary(a_frames), "b": _summary(b_frames)},
        "reference": a_label, "subject": b_label, "unpairedIndices": unpaired,
        "letterboxPolicy": {"a": letterbox_policy_a or make_letterbox_policy(cfg),
                            "b": letterbox_policy_b or make_letterbox_policy(cfg)},
        "thresholdsMetOnAllFrames": all(v.get("thresholdsMet") for v in verdicts) if verdicts else False,
        "aggregate": agg, "config": _config_block(meta, overrides), "frames": verdicts,
    }
