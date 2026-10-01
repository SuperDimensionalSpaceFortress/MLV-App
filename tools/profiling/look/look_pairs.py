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
    * A ZERO-EFFECT CONTROL is added per run: the SAME image on both sides (also emitted order-swapped, and
      indistinguishable from a real pair to the judge). A judge that prefers a side of identical images is
      measured, not trusted.
    * The queue is shuffled by seed so a unit's two orderings are not adjacent.

The planning functions are pure stdlib; only compose_pair_image() needs Pillow (imported lazily).
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


def _h(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _first_order_a_left(seed, unit_id):
    """True when subject A sits LEFT in the unit's first ordering. Seed-determined."""
    return int(_h(seed, "first-order", unit_id)[0], 16) % 2 == 0


def plan_pairs(subject_a, subject_b, frame_ids, seed, controls=1):
    """Pure plan: list of item dicts (answer-key view). `subject_*` are {"name": str, "frames": {id: path}}.

    Items: for each frame id present in BOTH subjects, a real unit (2 orderings); then `controls` zero-effect
    units (identical image both sides, 2 orderings each) drawn from seed-chosen (subject, frame) pairs."""
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
    pool.sort(key=lambda sf: _h(seed, "control-pick", sf[0]["name"], sf[1]))
    for k in range(min(controls, len(pool))):
        subject, frame = pool[k]
        unit_id = f"control-{k}"
        for order in (1, 2):
            side = {"subject": subject["name"], "frame": frame, "path": subject["frames"][frame]}
            items.append({"unitId": unit_id, "kind": "control", "order": order, "frame": frame,
                          "left": dict(side), "right": dict(side)})
    for item in items:
        item["itemId"] = "p-" + _h(seed, "item", item["unitId"], item["order"])[:12]
    items.sort(key=lambda it: _h(seed, "queue", it["itemId"]))
    if len({it["itemId"] for it in items}) != len(items):
        raise ValueError("item id collision (change the seed)")
    return {"items": items, "droppedFrameIds": missing}


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


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def build_session(subject_a, subject_b, frame_ids, seed, out_dir, rubric_sha256, controls=1, created_utc=None):
    """Write pair images + the three JSON files into out_dir; return their paths.

    judge_manifest.json   judge-facing: item ids + image file names. Nothing else.
    answer_key.json       NOT judge-facing: subjects, slots, seed, source sha256s.
    session.json          the frozen-before-judging record: rubricSha256, seed, imageSha256s, item count."""
    plan = plan_pairs(subject_a, subject_b, frame_ids, seed, controls=controls)
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    judge_items, key_items, image_hashes = [], [], []
    for item in plan["items"]:
        name = item["itemId"] + ".png"
        path = os.path.join(images_dir, name)
        compose_pair_image(item["left"]["path"], item["right"]["path"], path)
        pair_sha = sha256_file(path)
        image_hashes.append(pair_sha)
        judge_items.append({"itemId": item["itemId"], "image": f"images/{name}"})
        key_items.append({
            "itemId": item["itemId"], "unitId": item["unitId"], "kind": item["kind"], "order": item["order"],
            "frame": item["frame"], "pairImageSha256": pair_sha,
            "left": {"subject": item["left"]["subject"], "sourceSha256": sha256_file(item["left"]["path"])},
            "right": {"subject": item["right"]["subject"], "sourceSha256": sha256_file(item["right"]["path"])},
        })
    manifest = {"schema": SCHEMA_JUDGE_MANIFEST, "items": judge_items}
    key = {"schema": SCHEMA_ANSWER_KEY, "orderSeed": seed, "subjects": [subject_a["name"], subject_b["name"]],
           "droppedFrameIds": plan["droppedFrameIds"], "items": key_items}
    session = {
        "schema": SCHEMA_SESSION, "rubricSha256": rubric_sha256, "orderSeed": seed,
        "itemCount": len(judge_items), "imageSha256s": sorted(image_hashes), "createdUtc": created_utc,
        "gutter": {"rgb": list(GUTTER_RGB), "px": GUTTER_PX, "marginPx": MARGIN_PX},
        "emittedTwiceOrderSwapped": True, "controlUnits": controls,
    }
    paths = {}
    for fname, doc in (("judge_manifest.json", manifest), ("answer_key.json", key), ("session.json", session)):
        paths[fname] = os.path.join(out_dir, fname)
        with open(paths[fname], "w", encoding="utf-8", newline="\n") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
    return paths
