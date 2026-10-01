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
    * N/A is never a pass. With no skin-tone region (or no baseline frame) the skin check is NOT_APPLICABLE
      and the sheet verdict says how many frames it actually applied to.
    * Letterbox-aware. A playback capture can include black window bars (the Ultra-Magnus CUDA fixture tiles
      are 16:9 with ~17% black bars; the CPU tiles are content-only). Bars are excluded from every percentage
      and the exclusion is written into the verdict, otherwise every CUDA frame fails 'crushed shadows' on
      geometry rather than look. A would-be exclusion of more than 50% is refused as 'probably a dark frame'.
    * Thresholds live in ONE tracked file with a reason per value (look_floor_config.json); a verdict carries
      the config's sha256 and any override that was applied.

Requires Pillow + numpy (the same dependency make-contact-sheet.py already has). Pure-stdlib pieces live in
look_config.py / look_tally.py so CI images without numpy still prove them.
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


def detect_letterbox(arr, cfg):
    """Find black bars touching the frame edges. Returns a dict; 'active' is (top, bottom, left, right) crop
    sizes that were APPLIED (all zero when nothing was detected or the exclusion was refused)."""
    lb = cfg["letterbox"]
    height, width = arr.shape[:2]
    result = {"detected": False, "top": 0, "bottom": 0, "left": 0, "right": 0, "excludedPct": 0.0, "refused": None}
    if not lb["detect"]:
        result["refused"] = "DETECTION_DISABLED"
        return result
    bar_max = int(lb["bar_max_code"])
    row_is_bar = arr.max(axis=(1, 2)) <= bar_max
    if row_is_bar.all():
        result["refused"] = "ALL_BLACK_FRAME"
        return result

    def leading(flags):
        return int(np.argmin(flags)) if not flags.all() else len(flags)

    top = leading(row_is_bar)
    bottom = leading(row_is_bar[::-1])
    rows = arr[top:height - bottom]
    col_is_bar = rows.max(axis=(0, 2)) <= bar_max
    left = leading(col_is_bar)
    right = leading(col_is_bar[::-1])
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
    result.update(
        {"detected": bool(top or bottom or left or right), "top": top, "bottom": bottom, "left": left,
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
    return {"regionPct": round(pct, 4), "usable": usable, "meanHueDeg": hue_deg}


# ---------------------------------------------------------------------------------------------------------
# per-frame floor
# ---------------------------------------------------------------------------------------------------------

def _check(value, threshold, comparator):
    ok = value <= threshold if comparator == "<=" else value >= threshold
    return {"value": round(float(value), 6), "threshold": threshold, "comparator": comparator,
            "outcome": PASS if ok else FAIL}


def evaluate_frame(source, cfg, reference=None, frame_id=None):
    """Floor verdict for ONE frame. `reference` (optional) is the baseline frame for the skin-hue drift."""
    verdict = {"frameId": frame_id, "outcome": NOT_EVALUABLE, "checks": {}, "metrics": {}, "geometry": {}}
    try:
        arr = load_rgb(source)
    except Exception as exc:  # unreadable / wrong type: a typed terminal, never a silent skip
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        return verdict
    letterbox = detect_letterbox(arr, cfg)
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
    skin_check = {"outcome": NOT_APPLICABLE, "threshold": frame_cfg["skin_hue_drift_deg_max"], "comparator": "<="}
    if reference is None:
        skin_check["reason"] = "NO_BASELINE_FRAME"
    elif not skin["usable"]:
        skin_check["reason"] = "NO_SKIN_TONE_REGION_IN_FRAME"
    else:
        try:
            ref_arr = load_rgb(reference)
            ref_active = crop_active(ref_arr, detect_letterbox(ref_arr, cfg))
            ref_skin = skin_summary(ref_active, cfg)
        except Exception as exc:
            ref_skin = None
            skin_check["reason"] = f"BASELINE_UNREADABLE: {type(exc).__name__}"
        if ref_skin is not None:
            if not ref_skin["usable"]:
                skin_check["reason"] = "NO_SKIN_TONE_REGION_IN_BASELINE"
            else:
                drift = hue_distance_deg(skin["meanHueDeg"], ref_skin["meanHueDeg"])
                skin_check = _check(drift, frame_cfg["skin_hue_drift_deg_max"], "<=")
                skin_check["baselineMeanHueDeg"] = ref_skin["meanHueDeg"]
    checks["skin_hue_drift"] = skin_check

    verdict["metrics"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}
    verdict["checks"] = checks
    failed = sorted(name for name, c in checks.items() if c["outcome"] == FAIL)
    verdict["failedChecks"] = failed
    verdict["outcome"] = FAIL if failed else PASS
    return verdict


def index_frames(directory):
    """{index: path} for the PNGs of a frames dir; the index is the LAST run of digits in the stem."""
    found = {}
    for name in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(name)
        if ext.lower() != ".png":
            continue
        digits = re.findall(r"\d+", stem)
        if not digits:
            continue
        found[int(digits[-1])] = os.path.join(directory, name)
    return found


def _config_block(meta, overrides):
    return {"configSha256": meta["configSha256"], "configVersion": meta.get("configVersion"),
            "overrides": overrides or {}}


def evaluate_sheet(frames, cfg, meta, label="", references=None, overrides=None):
    """Sheet verdict. `frames` = {index: path-or-array}; `references` = {index: baseline} for the skin drift."""
    references = references or {}
    frame_verdicts = []
    for index in sorted(frames):
        v = evaluate_frame(frames[index], cfg, reference=references.get(index), frame_id=index)
        if isinstance(frames[index], str) and os.path.isfile(frames[index]):
            v["imageSha256"] = look_config.sha256_file(frames[index])
        frame_verdicts.append(v)
    counts = {PASS: 0, FAIL: 0, NOT_EVALUABLE: 0}
    for v in frame_verdicts:
        counts[v["outcome"]] += 1
    skin_applicable = sum(1 for v in frame_verdicts if v["checks"].get("skin_hue_drift", {}).get("outcome") in (PASS, FAIL))
    if not frame_verdicts or counts[NOT_EVALUABLE]:
        outcome = INCOMPLETE
    elif counts[FAIL]:
        outcome = FAIL
    else:
        outcome = PASS
    return {
        "schema": SCHEMA_FRAME_SHEET, "kind": "floor-sheet", "subject": label, "outcome": outcome,
        "frameCount": len(frame_verdicts), "counts": counts,
        "skinCheck": {"appliedToFrames": skin_applicable, "ofFrames": len(frame_verdicts)},
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


def compare_frames(a_src, b_src, cfg, scope, frame_id=None):
    """Pair verdict for one frame index. a = reference backend, b = subject backend (named by the caller)."""
    if scope not in cfg["pair"]["scopes"]:
        raise ValueError(f"unknown scope {scope!r}; known: {sorted(cfg['pair']['scopes'])}")
    scope_cfg = cfg["pair"]["scopes"][scope]
    out = {"frameId": frame_id, "scope": scope, "gated": bool(scope_cfg["gated"]), "outcome": INCOMPLETE}
    try:
        a_full, b_full = load_rgb(a_src), load_rgb(b_src)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    a = crop_active(a_full, detect_letterbox(a_full, cfg))
    b = crop_active(b_full, detect_letterbox(b_full, cfg))
    out["geometry"] = {"aActive": [int(a.shape[1]), int(a.shape[0])], "bActive": [int(b.shape[1]), int(b.shape[0])]}
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


def compare_sheets(a_frames, b_frames, cfg, meta, scope, a_label="a", b_label="b", overrides=None):
    """Pair two frame sets by index. Any unpaired index, or any frame that could not be compared, makes the
    sheet INCOMPLETE -- a missing side is never read as agreement (DESIGN.md P2)."""
    indices = sorted(set(a_frames) | set(b_frames))
    unpaired = [i for i in indices if i not in a_frames or i not in b_frames]
    verdicts = [compare_frames(a_frames[i], b_frames[i], cfg, scope, frame_id=i)
                for i in indices if i not in unpaired]
    gated = bool(cfg["pair"]["scopes"][scope]["gated"])
    broken = [v for v in verdicts if v["outcome"] in (INCOMPLETE, GEOMETRY_MISMATCH)]
    if not verdicts or unpaired or broken:
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
        "reference": a_label, "subject": b_label, "unpairedIndices": unpaired,
        "thresholdsMetOnAllFrames": all(v.get("thresholdsMet") for v in verdicts) if verdicts else False,
        "aggregate": agg, "config": _config_block(meta, overrides), "frames": verdicts,
    }
