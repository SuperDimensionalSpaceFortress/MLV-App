#!/usr/bin/env python3
"""Command line for the Look Assist objective floor + blind judge harness. See README.md for the workflow.

    python tools/profiling/look/look_cli.py tiles        --sheet S.png --out-dir D --prefix cuda --count 6
    python tools/profiling/look/look_cli.py floor        --frames-dir D [--baseline-dir B] --label cuda --out V.json
                                                         [--letterbox auto-symmetric | --letterbox-bars top=34,bottom=34]
    python tools/profiling/look/look_cli.py pair-metrics --a-dir A --b-dir B --scope shader-subset --out V.json
                                                         [--letterbox auto-symmetric] [--a-letterbox-bars ..] [--b-letterbox-bars ..]
    python tools/profiling/look/look_cli.py verify-rubric
    python tools/profiling/look/look_cli.py probe-codex [--live-vision-dir DIR]
    python tools/profiling/look/look_cli.py build-session --a cuda=DIR --b cpu=DIR --seed N --out-dir SESS
                                                         [--letterbox auto-symmetric] [--a-letterbox-bars ..] [--b-letterbox-bars ..]
    python tools/profiling/look/look_cli.py judge        --session-dir SESS --runner claude:MODEL --producer-model M
    python tools/profiling/look/look_cli.py tally        --session-dir SESS --results R.json --producer-model M --out T.json
    python tools/profiling/look/look_cli.py judge-disagreement --entries T1.json T2.json [--out D.json]

Exit codes: 0 verdict PASS/REPORTED, a usable tally, or a non-verdict command that succeeded; 1 verdict FAIL;
2 verdict INCOMPLETE, a structural error, or a tally entry that is NOT usable (its JSON is still written).
A verdict is data first: the JSON is written even when the exit code is non-zero.
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import look_config  # noqa: E402
import look_judges  # noqa: E402
import look_pairs  # noqa: E402
import look_tally  # noqa: E402

# contact-sheet geometry written by make-contact-sheet.py (HEADER_HEIGHT, TILE_PADDING, TILE_TARGET_WIDTH,
# TILE_LABEL_HEIGHT); the tile background is (24, 24, 24).
SHEET_HEADER_PX, SHEET_PAD_PX, SHEET_TILE_W, SHEET_LABEL_PX = 150, 6, 480, 22


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def extract_sheet_tiles(sheet_path, out_dir, prefix, count, cols=4):
    """FIXTURE DEMO ONLY: cut the tile pictures back out of a contact-sheet PNG (header, label strip and the tile
    background removed). Real runs use the full-resolution frame-NN.png of --contact-sheet-dir; a sheet tile is a
    480-px downscale, so numbers from it describe the thumbnail, and the verdict's source says so."""
    import numpy as np
    from PIL import Image

    with Image.open(sheet_path) as handle:
        sheet = np.asarray(handle.convert("RGB"))
    rows = (count + cols - 1) // cols
    body_h = sheet.shape[0] - SHEET_HEADER_PX - SHEET_PAD_PX
    tile_h = body_h // rows - SHEET_PAD_PX
    if tile_h <= SHEET_LABEL_PX:
        raise ValueError("sheet geometry does not match make-contact-sheet.py's layout")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for i in range(count):
        col, row = i % cols, i // cols
        x = SHEET_PAD_PX + col * (SHEET_TILE_W + SHEET_PAD_PX)
        y = SHEET_HEADER_PX + SHEET_PAD_PX + row * (tile_h + SHEET_PAD_PX)
        tile = sheet[y:y + tile_h - SHEET_LABEL_PX, x:x + SHEET_TILE_W]
        not_bg = (tile != 24).any(axis=2)  # exact tile background (24,24,24) only
        if not not_bg.any():
            raise ValueError(f"tile {i} is empty")
        ys, xs = np.where(not_bg)
        tile = tile[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        path = os.path.join(out_dir, f"{prefix}-frame-{i:02d}.png")
        Image.fromarray(np.ascontiguousarray(tile), "RGB").save(path)
        written.append(path)
    return written


def _apply_overrides(cfg, args):
    overrides = {}
    tol = getattr(args, "geometry_tolerance_px", None)
    if tol is not None:
        if not getattr(args, "override_reason", None):
            raise SystemExit("--geometry-tolerance-px needs --override-reason (a threshold override without a reason is a guess)")
        overrides["pair.geometry_tolerance_px"] = {
            "from": cfg["pair"]["geometry_tolerance_px"], "to": tol, "reason": args.override_reason}
        cfg["pair"]["geometry_tolerance_px"] = tol
    return overrides


def cmd_tiles(args):
    paths = extract_sheet_tiles(args.sheet, args.out_dir, args.prefix, args.count, args.cols)
    print(json.dumps({"written": len(paths), "dir": args.out_dir}))
    return 0


def _exit_for(outcome):
    return {"PASS": 0, "REPORTED": 0, "FAIL": 1}.get(outcome, 2)


def _policy(cfg, mode, bars_spec):
    """The caller's letterbox decision for ONE capture (see look_metrics.make_letterbox_policy)."""
    import look_metrics

    declared = look_metrics.parse_declared_bars(bars_spec) if bars_spec else None
    return look_metrics.make_letterbox_policy(cfg, mode=mode, declared=declared)


def cmd_floor(args):
    import look_metrics

    cfg, meta = look_config.load_config(args.config)
    policy = _policy(cfg, args.letterbox, args.letterbox_bars)
    frames = look_metrics.index_frames(args.frames_dir)
    baselines = look_metrics.index_frames(args.baseline_dir) if args.baseline_dir else {}
    verdict = look_metrics.evaluate_sheet(frames, cfg, meta, label=args.label, references=baselines,
                                          letterbox_policy=policy)
    verdict["createdUtc"] = _now()
    verdict["source"] = args.source_note
    _write_json(args.out, verdict)
    print(f"floor {args.label}: {verdict['outcome']} frames={verdict['frameCount']} counts={verdict['counts']} "
          f"skinCheckApplied={verdict['skinCheck']['appliedToFrames']}/{verdict['skinCheck']['ofFrames']}")
    return _exit_for(verdict["outcome"])


def cmd_pair_metrics(args):
    import look_metrics

    cfg, meta = look_config.load_config(args.config)
    overrides = _apply_overrides(cfg, args)
    a = look_metrics.index_frames(args.a_dir)
    b = look_metrics.index_frames(args.b_dir)
    verdict = look_metrics.compare_sheets(
        a, b, cfg, meta, args.scope, args.a_label, args.b_label, overrides,
        letterbox_policy_a=_policy(cfg, args.letterbox, args.a_letterbox_bars),
        letterbox_policy_b=_policy(cfg, args.letterbox, args.b_letterbox_bars))
    verdict["createdUtc"] = _now()
    verdict["source"] = args.source_note
    _write_json(args.out, verdict)
    agg = verdict["aggregate"] or {}
    print(f"pair-metrics {args.a_label} vs {args.b_label} scope={args.scope}: {verdict['outcome']} "
          f"thresholdsMetOnAllFrames={verdict['thresholdsMetOnAllFrames']} "
          f"meanSsim={agg.get('meanSsimLuma')} meanMismatch={agg.get('meanMismatchFraction')}")
    return _exit_for(verdict["outcome"])


def cmd_verify_rubric(_args):
    lock = look_config.verify_rubric_lock()
    print(json.dumps({"rubricId": lock["rubricId"], "rubricSha256": lock["rubricSha256"], "verified": True}))
    return 0


def cmd_probe_codex(args):
    probe = look_judges.probe_codex_image_support()
    if args.live_vision_dir and probe["status"] == look_judges.CROSS_FAMILY_FLAG_ONLY:
        import numpy as np
        from PIL import Image

        os.makedirs(args.live_vision_dir, exist_ok=True)
        png = os.path.join(args.live_vision_dir, "vision-probe.png")
        arr = np.zeros((96, 160, 3), dtype=np.uint8)
        arr[:, :] = (200, 30, 30)       # red field
        arr[28:68, 60:100] = (20, 200, 40)  # green square: the answer exists only in the pixels
        Image.fromarray(arr, "RGB").save(png)
        probe["liveVision"] = look_judges.probe_codex_vision(png, "green", model=args.model)
        probe["status"] = probe["liveVision"]["status"]
    probe["createdUtc"] = _now()
    if args.out:
        _write_json(args.out, probe)
    print(json.dumps(probe, indent=2))
    return 0


def _parse_subject(spec):
    name, _, directory = spec.partition("=")
    if not name or not directory:
        raise SystemExit(f"subject must be NAME=DIR, got {spec!r}")
    return name, directory


def _prepare_subject_frames(name, directory, out_dir, cfg, policy=None):
    """Crop what the caller DECLARED (or opted in to auto-detecting symmetrically) as letterbox bars -- a bar is a
    tell for which backend rendered a frame -- into out_dir; returns {frame index: path}. Undeclared dark bands are
    left in the picture, so the judges also see a crushed region instead of having it cropped away. Source frames
    stay untouched."""
    import numpy as np
    import look_metrics
    from PIL import Image

    prepared = {}
    os.makedirs(out_dir, exist_ok=True)
    for index, path in look_metrics.index_frames(directory).items():
        arr = look_metrics.load_rgb(path)
        active = look_metrics.crop_active(arr, look_metrics.detect_letterbox(arr, cfg, policy))
        target = os.path.join(out_dir, f"{name}-{index:02d}.png")
        Image.fromarray(np.ascontiguousarray(active), "RGB").save(target)
        prepared[index] = target
    return prepared


def _common_crop(a_frames, b_frames, tolerance_px, name_a="a", name_b="b"):
    """Centre-crop each shared frame pair to a common size when the sizes differ by <= tolerance. Returns
    (kept indices, dropped) where dropped = [{"frameId", "reason"}]: a frame that is not kept is never silent."""
    import numpy as np
    from PIL import Image

    keep, dropped = [], []
    for index in sorted(set(a_frames) ^ set(b_frames)):
        only = name_a if index in a_frames else name_b
        dropped.append({"frameId": index, "reason": f"UNSHARED: frame exists only in {only}"})
    for index in sorted(set(a_frames) & set(b_frames)):
        with Image.open(a_frames[index]) as ia, Image.open(b_frames[index]) as ib:
            wa, ha, wb, hb = ia.width, ia.height, ib.width, ib.height
        if abs(wa - wb) > tolerance_px or abs(ha - hb) > tolerance_px:
            dropped.append({"frameId": index, "reason": (
                f"CROP_TOLERANCE_EXCEEDED: {name_a} {wa}x{ha} vs {name_b} {wb}x{hb}, tolerance {tolerance_px}px")})
            continue
        if (wa, ha) != (wb, hb):
            w, h = min(wa, wb), min(ha, hb)
            for frames in (a_frames, b_frames):
                with Image.open(frames[index]) as im:
                    arr = np.asarray(im.convert("RGB"))
                t, l = (arr.shape[0] - h) // 2, (arr.shape[1] - w) // 2
                Image.fromarray(np.ascontiguousarray(arr[t:t + h, l:l + w]), "RGB").save(frames[index])
        keep.append(index)
    return keep, dropped


def cmd_build_session(args):
    cfg, _ = look_config.load_config(args.config)
    lock = look_config.verify_rubric_lock()  # the digest is recorded BEFORE any image exists
    name_a, dir_a = _parse_subject(args.a)
    name_b, dir_b = _parse_subject(args.b)
    prep = os.path.join(args.out_dir, "source-frames")
    fa = _prepare_subject_frames(name_a, dir_a, prep, cfg, _policy(cfg, args.letterbox, args.a_letterbox_bars))
    fb = _prepare_subject_frames(name_b, dir_b, prep, cfg, _policy(cfg, args.letterbox, args.b_letterbox_bars))
    keep, dropped = _common_crop(fa, fb, args.common_crop_tolerance_px, name_a, name_b)
    if args.frame_ids:
        wanted = [int(x) for x in args.frame_ids.split(",")]
        dropped += [{"frameId": i, "reason": "NOT_IN_--frame-ids"} for i in keep if i not in wanted]
        known = {d["frameId"] for d in dropped} | set(keep)
        dropped += [{"frameId": i, "reason": "REQUESTED_BUT_NOT_PRESENT_IN_EITHER_SUBJECT"}
                    for i in wanted if i not in known]
        keep = [i for i in wanted if i in keep]
    if args.max_frames:
        dropped += [{"frameId": i, "reason": f"BEYOND_--max-frames={args.max_frames}"} for i in keep[args.max_frames:]]
        keep = keep[:args.max_frames]
    paths = look_pairs.build_session(
        {"name": name_a, "frames": fa}, {"name": name_b, "frames": fb}, keep, args.seed, args.out_dir,
        lock["rubricSha256"], controls=args.controls, positive_controls=args.positive_controls,
        created_utc=_now(), dropped_frames=dropped, crop_tolerance_px=args.common_crop_tolerance_px)
    dropped_sorted = sorted(dropped, key=lambda d: d["frameId"])
    if dropped_sorted:
        print(f"[look_cli] WARNING {len(dropped_sorted)} frame(s) were NOT judged: "
              + "; ".join(f"{d['frameId']} ({d['reason']})" for d in dropped_sorted), file=sys.stderr)
    print(json.dumps({"frames": keep, "droppedFrames": dropped_sorted,
                      "files": {k: os.path.basename(v) for k, v in paths.items()}}))
    return 0


def _runner_from_spec(spec, args):
    kind, _, model = spec.partition(":")
    if kind == "claude":
        return look_judges.ClaudeCliJudge(model, judge_id=args.judge_id, timeout_s=args.item_timeout_s)
    if kind == "codex":
        return look_judges.CodexExecJudge(model or None, judge_id=args.judge_id, timeout_s=args.item_timeout_s)
    raise SystemExit(f"--runner must be claude:MODEL or codex:MODEL, got {spec!r}")


def cmd_judge(args):
    runner = _runner_from_spec(args.runner, args)
    look_judges.assert_not_producer(runner.model, args.producer_model)  # the alias-table, family-level guard
    summary = look_judges.run_session(
        args.session_dir, runner, workers=args.workers, deadline_s=args.deadline_s, max_items=args.max_items)
    print(json.dumps(summary))
    return 0 if summary["remaining"] == 0 and summary["errors"] == 0 else 2


def _current_image_digests(session_dir):
    """{itemId: sha256 of the pair image as it is on disk NOW}. An item whose image is missing maps to None, which
    never equals a recorded digest, so a deleted image reads as a changed one."""
    manifest = _read_json(os.path.join(session_dir, "judge_manifest.json"))
    digests = {}
    for entry in manifest["items"]:
        try:
            digests[entry["itemId"]] = look_judges.sha256_file(os.path.join(session_dir, entry["image"]))
        except OSError:
            digests[entry["itemId"]] = None
    return digests


def cmd_tally(args):
    key = _read_json(os.path.join(args.session_dir, "answer_key.json"))
    session = _read_json(os.path.join(args.session_dir, "session.json"))
    results = _read_json(args.results)
    cfg, _ = look_config.load_config(args.config)
    entry = look_tally.tally(
        key, results["items"], results["judge"], session, cfg,
        current_image_sha256=_current_image_digests(args.session_dir), forbidden_models=args.producer_model,
        cross_family_status=args.cross_family_status)
    entry["errorsDuringJudging"] = results.get("errors", {})
    entry["staleRejectedAtJudging"] = results.get("staleRejected", {})
    entry["createdUtc"] = _now()
    _write_json(args.out, entry)
    print(f"tally {entry['judgeId']}: winner={entry['preference']['winner']} usable={entry['usable']} "
          f"consistentUnits={entry['preference']['consistentUnits']} flips={entry['preference']['discardedFlips']} "
          f"control={entry['controlResult']['outcome']} positiveControl={entry['positiveControlResult']['outcome']} "
          f"slotBias={entry['slotTally']['slotBias']} unusableReasons={entry['unusableReasons']}")
    return 0 if entry["usable"] else 2


def cmd_judge_disagreement(args):
    cfg, _ = look_config.load_config(args.config)
    entries = [_read_json(path) for path in args.entries]
    verdict = look_tally.judge_disagreement(entries, cfg["judge_disagreement"]["third_judge_points"])
    verdict["createdUtc"] = _now()
    if args.out:
        _write_json(args.out, verdict)
    print(json.dumps({k: verdict[k] for k in ("thirdJudgeNeeded", "thresholdPoints", "comparable", "unusableJudges")}))
    return 0 if verdict["comparable"] else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("tiles")
    s.add_argument("--sheet", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--prefix", required=True)
    s.add_argument("--count", type=int, required=True)
    s.add_argument("--cols", type=int, default=4)
    s.set_defaults(fn=cmd_tiles)

    letterbox_help = ("what the floor may hide: `off` measures the full frame; `auto-symmetric` excludes dark bands "
                      "only when top/bottom (or left/right) match within the config's symmetry tolerance. "
                      "Default: the config's letterbox.auto_exclude_symmetric (off as shipped).")
    bars_help = "bars you KNOW exist on this capture, e.g. top=34,bottom=34 (each must really be dark, else refused)"

    s = sub.add_parser("floor")
    s.add_argument("--frames-dir", required=True)
    s.add_argument("--baseline-dir")
    s.add_argument("--label", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--config")
    s.add_argument("--source-note", default="")
    s.add_argument("--letterbox", choices=["off", "auto-symmetric"], help=letterbox_help)
    s.add_argument("--letterbox-bars", help=bars_help)
    s.set_defaults(fn=cmd_floor)

    s = sub.add_parser("pair-metrics")
    s.add_argument("--a-dir", required=True)
    s.add_argument("--b-dir", required=True)
    s.add_argument("--a-label", default="a")
    s.add_argument("--b-label", default="b")
    s.add_argument("--scope", required=True, choices=["shader-subset", "full-look"])
    s.add_argument("--out", required=True)
    s.add_argument("--config")
    s.add_argument("--letterbox", choices=["off", "auto-symmetric"], help=letterbox_help)
    s.add_argument("--a-letterbox-bars", help="A side: " + bars_help)
    s.add_argument("--b-letterbox-bars", help="B side: " + bars_help)
    s.add_argument("--geometry-tolerance-px", type=int)
    s.add_argument("--override-reason")
    s.add_argument("--source-note", default="")
    s.set_defaults(fn=cmd_pair_metrics)

    s = sub.add_parser("verify-rubric")
    s.set_defaults(fn=cmd_verify_rubric)

    s = sub.add_parser("probe-codex")
    s.add_argument("--live-vision-dir")
    s.add_argument("--model")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_probe_codex)

    s = sub.add_parser("build-session")
    s.add_argument("--a", required=True)
    s.add_argument("--b", required=True)
    s.add_argument("--seed", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--controls", type=int, default=1)
    s.add_argument("--positive-controls", type=int, default=1,
                   help="units of original-vs-known-degradation the judge must prefer the original on (0 makes the session unusable)")
    s.add_argument("--letterbox", choices=["off", "auto-symmetric"], help=letterbox_help)
    s.add_argument("--a-letterbox-bars", help="A side: " + bars_help)
    s.add_argument("--b-letterbox-bars", help="B side: " + bars_help)
    s.add_argument("--max-frames", type=int)
    s.add_argument("--frame-ids", help="comma-separated frame indices to judge (default: every shared frame)")
    s.add_argument("--common-crop-tolerance-px", type=int, default=2)
    s.add_argument("--config")
    s.set_defaults(fn=cmd_build_session)

    s = sub.add_parser("judge")
    s.add_argument("--session-dir", required=True)
    s.add_argument("--runner", required=True)
    s.add_argument("--judge-id")
    s.add_argument("--producer-model", action="append", default=[], required=True,
                   help="model id of the look's producer or the hub; repeatable; the judge may not match")
    s.add_argument("--workers", type=int, default=3)
    s.add_argument("--deadline-s", type=int, default=500)
    s.add_argument("--item-timeout-s", type=int, default=300)
    s.add_argument("--max-items", type=int)
    s.set_defaults(fn=cmd_judge)

    s = sub.add_parser("tally")
    s.add_argument("--session-dir", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--config")
    s.add_argument("--cross-family-status")
    s.add_argument("--producer-model", action="append", default=[], required=True,
                   help="model id of the look's producer or the hub; repeatable; the tally re-applies the guard")
    s.set_defaults(fn=cmd_tally)

    s = sub.add_parser("judge-disagreement")
    s.add_argument("--entries", nargs="+", required=True, help="tally entry JSON files, one per judge")
    s.add_argument("--out")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_judge_disagreement)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (look_config.ConfigError, look_config.RubricLockError, look_judges.ProducerJudgeError,
            look_judges.JudgeError, ValueError) as exc:
        print(f"[look_cli] ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
