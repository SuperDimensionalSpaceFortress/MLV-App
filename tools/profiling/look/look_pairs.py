#!/usr/bin/env python3
"""Blind pair builder for model judging of Look Assist frames (DESIGN.md AMENDMENT 2 B4).

WHAT BLINDING MEANS HERE
    * Two files per run. The JUDGE-FACING manifest lists opaque item ids and image paths ONLY. The ANSWER KEY
      (subject names, which one sat left, the seed) is written separately and never shown to a judge.
    * Subject names (flavor / backend) appear nowhere a judge can read: file names are opaque hashes, the PNG
      carries no text chunks, and the pair image has no label strip. The caller strips any header/label from
      the source frames before they get here (look_cli `tiles` does it for contact-sheet fixtures).
    * Left/right is decided by a RECORDED SEED, not by the order the caller listed the subjects. Every
      decision is a pure function of sha256(seed | unit | ...) so the plan is identical on every Python.
    * Every real unit is emitted TWICE, order-swapped. look_tally later discards a vote that flips with the swap.
    * A NEGATIVE CONTROL is added per run: the SAME image on both sides (also emitted order-swapped, and
      indistinguishable from a real pair to the judge). A judge that prefers a side of identical images is
      measured, not trusted.
    * A POSITIVE CONTROL is added per run: one real frame against a copy of itself with a large, known, deliberate
      degradation (flattened contrast + a strong colour wash). A judge that cannot prefer the original, in both
      orders, cannot see a difference that is plainly there, so an always-`tie` judge is unusable.
    * The queue is shuffled by seed so a unit's two orderings are not adjacent.
    * ITEM IDS ARE BOUND TO THE IMAGE: itemId = sha256(seed | unit | order | sha256 of the pair PNG). Rebuild a
      session over different frames and every id changes, so a verdict recorded against the old images can never
      be matched to a new one (look_judges.run_session and look_tally also compare the digests themselves).

The planning functions are pure stdlib; only compose_pair_image() / degrade_image() need Pillow (imported lazily).
"""
import hashlib
import json
import os

GUTTER_RGB = (128, 128, 128)  # mid-grey: neither a tonal anchor nor a hint
GUTTER_PX = 24
MARGIN_PX = 24

SCHEMA_SESSION = "mlv-app/look-judge-session/v1"
SCHEMA_ANSWER_KEY = "mlv-app/look-judge-answer-key/v1"
SCHEMA_JUDGE_MANIFEST = "mlv-app/look-judge-manifest/v1"

# The pseudo-subjects of a positive-control unit. They live only in the answer key.
POSITIVE_ORIGINAL = "@original"
POSITIVE_DEGRADED = "@degraded"

# The deliberate degradation of the positive control: large on purpose, so a judge that sees nothing here is
# not seeing. Contrast flattened to 45% and 40% of a strong purple-grey wash blended over the picture.
DEGRADE_CONTRAST = 0.45
DEGRADE_TINT_RGB = (150, 80, 185)
DEGRADE_BLEND = 0.40

KINDS = ("real", "control", "positive_control")


def _h(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def derive_item_id(seed, unit_id, order, pair_image_sha256):
    """The id of a built item: bound to the sha256 of the pair PNG it names."""
    return "p-" + _h(seed, "item", unit_id, order, pair_image_sha256)[:12]


def _first_order_a_left(seed, unit_id):
    """True when subject A sits LEFT in the unit's first ordering. Seed-determined."""
    return int(_h(seed, "first-order", unit_id)[0], 16) % 2 == 0


def plan_pairs(subject_a, subject_b, frame_ids, seed, controls=1, positive_controls=1):
    """Pure plan: list of item dicts (answer-key view). `subject_*` are {"name": str, "frames": {id: path}}.

    Items: for each frame id present in BOTH subjects, a real unit (2 orderings); then `controls` zero-effect
    units (identical image both sides) and `positive_controls` degraded-vs-original units, each 2 orderings,
    drawn from seed-chosen (subject, frame) pairs. The ids planned here are provisional (`itemId`): build_session
    replaces them with image-bound ids once the pair images exist."""
    if subject_a["name"] == subject_b["name"]:
        raise ValueError("a pair needs two different subjects")
    # Canonical order (by name) so the arrangement is a function of the seed alone, never of which subject the
    # caller happened to pass first: the same seed reproduces the same answer key from either call shape.
    subject_a, subject_b = sorted((subject_a, subject_b), key=lambda s: s["name"])
    common = [f for f in frame_ids if f in subject_a["frames"] and f in subject_b["frames"]]
    missing = [f for f in frame_ids if f not in common]
    if not common:
        raise ValueError("no frame id is present in both subjects")
    items = []
    for frame in common:
        unit_id = f"real-{frame}"
        a_left_first = _first_order_a_left(seed, unit_id)
        for order in (1, 2):
            a_left = a_left_first if order == 1 else not a_left_first
            left, right = (subject_a, subject_b) if a_left else (subject_b, subject_a)
            items.append({
                "unitId": unit_id, "kind": "real", "order": order, "frame": frame,
                "left": {"subject": left["name"], "frame": frame, "path": left["frames"][frame]},
                "right": {"subject": right["name"], "frame": frame, "path": right["frames"][frame]},
            })
    pool = [(s, f) for s in (subject_a, subject_b) for f in common]
    control_pool = sorted(pool, key=lambda sf: _h(seed, "control-pick", sf[0]["name"], sf[1]))
    for k in range(min(controls, len(control_pool))):
        subject, frame = control_pool[k]
        unit_id = f"control-{k}"
        for order in (1, 2):
            side = {"subject": subject["name"], "frame": frame, "path": subject["frames"][frame]}
            items.append({"unitId": unit_id, "kind": "control", "order": order, "frame": frame,
                          "left": dict(side), "right": dict(side)})
    positive_pool = sorted(pool, key=lambda sf: _h(seed, "positive-pick", sf[0]["name"], sf[1]))
    for k in range(min(positive_controls, len(positive_pool))):
        subject, frame = positive_pool[k]
        unit_id = f"positive-{k}"
        original_left_first = _first_order_a_left(seed, unit_id)
        for order in (1, 2):
            original_left = original_left_first if order == 1 else not original_left_first
            original = {"subject": POSITIVE_ORIGINAL, "frame": frame, "path": subject["frames"][frame]}
            degraded = {"subject": POSITIVE_DEGRADED, "frame": frame, "path": subject["frames"][frame],
                        "degrade": True}
            left, right = (original, degraded) if original_left else (degraded, original)
            items.append({"unitId": unit_id, "kind": "positive_control", "order": order, "frame": frame,
                          "left": left, "right": right})
    for item in items:
        item["itemId"] = "p-" + _h(seed, "item", item["unitId"], item["order"])[:12]
    items.sort(key=lambda it: _h(seed, "queue", it["itemId"]))
    if len({it["itemId"] for it in items}) != len(items):
        raise ValueError("item id collision (change the seed)")
    return {"items": items, "droppedFrameIds": missing,
            "droppedFrames": [{"frameId": f, "reason": "NOT_IN_BOTH_SUBJECTS"} for f in missing]}


def compose_pair_image(left_path, right_path, out_path):
    """Left | grey gutter | right on a mid-grey canvas. No text, no labels. Pictures of different sizes are
    padded onto a common cell (centred, never rescaled: rescaling would be a look change)."""
    from PIL import Image

    with Image.open(left_path) as li, Image.open(right_path) as ri:
        left, right = li.convert("RGB"), ri.convert("RGB")
    cell_w, cell_h = max(left.width, right.width), max(left.height, right.height)
    canvas = Image.new("RGB", (2 * MARGIN_PX + 2 * cell_w + GUTTER_PX, 2 * MARGIN_PX + cell_h), GUTTER_RGB)
    canvas.paste(left, (MARGIN_PX + (cell_w - left.width) // 2, MARGIN_PX + (cell_h - left.height) // 2))
    canvas.paste(right, (MARGIN_PX + cell_w + GUTTER_PX + (cell_w - right.width) // 2,
                         MARGIN_PX + (cell_h - right.height) // 2))
    canvas.save(out_path, "PNG")  # Pillow writes no text chunks unless asked
    return out_path


def degrade_image(src_path, out_path):
    """The positive control's known degradation (deterministic): flattened contrast + a strong colour wash."""
    from PIL import Image, ImageEnhance

    with Image.open(src_path) as handle:
        image = handle.convert("RGB")
    image = ImageEnhance.Contrast(image).enhance(DEGRADE_CONTRAST)
    image = Image.blend(image, Image.new("RGB", image.size, DEGRADE_TINT_RGB), DEGRADE_BLEND)
    image.save(out_path, "PNG")
    return out_path


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")


# Frames the CALLER chose to leave out. Everything else that is not judged is a LOSS (an unshared frame, a frame
# outside the crop tolerance, a requested id no subject has ...) and needs an explicit, recorded allowance. An
# unrecognised reason is a loss: the selection list is an allowlist.
SELECTION_DROP_REASONS = ("NOT_IN_--frame-ids", "BEYOND_--max-frames")
CAPTURE_REQUIRED_KEYS = ("configSha256", "letterboxPolicy", "frameCrops", "commonCrops")


def drop_is_selection(reason):
    return str(reason).startswith(SELECTION_DROP_REASONS)


def drop_policy(dropped, allowance=None):
    """What the session says about frames that were not judged: the ids lost, the ids the caller chose to omit,
    and the allowance (a reason, covering exactly the lost ids) if one was given. Used by build_session to WRITE the
    policy and by look_tally to RE-DERIVE it from the answer key."""
    loss = sorted({d["frameId"] for d in dropped if not drop_is_selection(d.get("reason"))})
    selection = sorted({d["frameId"] for d in dropped if drop_is_selection(d.get("reason"))} - set(loss))
    return {"lossFrameIds": loss, "selectionFrameIds": selection,
            "allowance": None if allowance is None else {"reason": allowance, "frameIds": loss}}


def build_session(subject_a, subject_b, frame_ids, seed, out_dir, rubric_sha256, controls=1, positive_controls=1,
                  created_utc=None, dropped_frames=None, crop_tolerance_px=None, compose=None, degrade=None,
                  capture=None, drop_allowance=None):
    """Write pair images + the three JSON files into out_dir; return their paths.

    judge_manifest.json   judge-facing: item ids + image file names. Nothing else.
    answer_key.json       NOT judge-facing: subjects, slots, seed, source + pair-image sha256s, dropped frames.
    session.json          the frozen-before-judging record: rubricSha256, seed, imageSha256s, item count, the
                          crop tolerance and every frame that was dropped (with the reason).

    `dropped_frames` = [{"frameId", "reason"}] the caller already removed (unshared, outside the crop tolerance,
    not requested ...); the plan adds the frames missing from a subject. A dropped frame is never silent.

    `capture` is how the frames were PREPARED (config digest, letterbox policy per subject, per-frame crops, common
    crops); it is written to session.json and answer_key.json and look_tally refuses a session that has none.
    `drop_allowance` is the explicit, reasoned acknowledgement that frames in the LOSS class were not judged; without
    one the tally calls the session unusable.

    `compose` / `degrade` default to the Pillow drawers; tests inject byte-level fakes so the whole binding logic
    (ids bound to image digests, key, session) runs on hosts and in tests that draw no pixels."""
    if drop_allowance is not None and (not isinstance(drop_allowance, str) or not drop_allowance.strip()):
        raise ValueError("a dropped-frame allowance needs a non-empty reason (a string saying why the loss is acceptable)")
    compose = compose or compose_pair_image
    degrade = degrade or degrade_image
    plan = plan_pairs(subject_a, subject_b, frame_ids, seed, controls=controls, positive_controls=positive_controls)
    images_dir = os.path.join(out_dir, "images")
    degraded_dir = os.path.join(out_dir, "degraded-sources")
    os.makedirs(images_dir, exist_ok=True)
    judge_items, key_items, image_hashes = [], [], []
    for item in plan["items"]:
        sides = {}
        for side in ("left", "right"):
            path = item[side]["path"]
            if item[side].get("degrade"):
                os.makedirs(degraded_dir, exist_ok=True)
                degraded_path = os.path.join(degraded_dir, f"{item['unitId']}.png")
                degrade(path, degraded_path)
                path = degraded_path
            sides[side] = path
        staging = os.path.join(images_dir, item["itemId"] + ".staging.png")
        compose(sides["left"], sides["right"], staging)
        pair_sha = sha256_file(staging)
        final_id = derive_item_id(seed, item["unitId"], item["order"], pair_sha)
        name = final_id + ".png"
        os.replace(staging, os.path.join(images_dir, name))
        image_hashes.append(pair_sha)
        judge_items.append({"itemId": final_id, "image": f"images/{name}"})
        key_items.append({
            "itemId": final_id, "unitId": item["unitId"], "kind": item["kind"], "order": item["order"],
            "frame": item["frame"], "pairImageSha256": pair_sha,
            "left": {"subject": item["left"]["subject"], "sourceSha256": sha256_file(sides["left"])},
            "right": {"subject": item["right"]["subject"], "sourceSha256": sha256_file(sides["right"])},
        })
    dropped = list(plan["droppedFrames"]) + [dict(d) for d in (dropped_frames or [])]
    manifest = {"schema": SCHEMA_JUDGE_MANIFEST, "items": judge_items}
    key = {"schema": SCHEMA_ANSWER_KEY, "orderSeed": seed, "subjects": [subject_a["name"], subject_b["name"]],
           "capture": capture,
           "droppedFrameIds": sorted({d["frameId"] for d in dropped}), "droppedFrames": dropped, "items": key_items,
           "positiveControl": {"original": POSITIVE_ORIGINAL, "degraded": POSITIVE_DEGRADED,
                               "contrast": DEGRADE_CONTRAST, "tintRgb": list(DEGRADE_TINT_RGB),
                               "blend": DEGRADE_BLEND}}
    session = {
        "schema": SCHEMA_SESSION, "rubricSha256": rubric_sha256, "orderSeed": seed,
        "itemCount": len(judge_items), "imageSha256s": sorted(image_hashes), "createdUtc": created_utc,
        "gutter": {"rgb": list(GUTTER_RGB), "px": GUTTER_PX, "marginPx": MARGIN_PX},
        "emittedTwiceOrderSwapped": True, "controlUnits": controls, "positiveControlUnits": positive_controls,
        "itemIdBinding": "sha256(seed|item|unit|order|pairImageSha256)[:12]",
        "commonCropTolerancePx": crop_tolerance_px, "droppedFrames": dropped,
        "droppedFramePolicy": drop_policy(dropped, drop_allowance), "capture": capture,
    }
    paths = {}
    for fname, doc in (("judge_manifest.json", manifest), ("answer_key.json", key), ("session.json", session)):
        paths[fname] = os.path.join(out_dir, fname)
        _write_json(paths[fname], doc)
    return paths
