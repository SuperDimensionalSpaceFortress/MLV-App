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
                                                         --seal-key-file K.txt | --no-seal     (sealed by default; the key goes to K.txt, never printed)
    python tools/profiling/look/look_cli.py judge        --session-dir SESS --runner claude:MODEL --producer-model M
    python tools/profiling/look/look_cli.py tally        --session-dir SESS --results R.json --producer-model M --out T.json
                                                         --canary C.json --seal-key K | --seal-key-file K.txt | env LOOK_SEAL_KEY
    python tools/profiling/look/look_cli.py judge-disagreement --entries T1.json T2.json [--out D.json] [--require-cross-family]
    python tools/profiling/look/look_cli.py unseal       --session-dir SESS --out-dir AUDIT --seal-key K
    python tools/profiling/look/look_cli.py isolation-canary --runner claude|codex [--out R.json]

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
import look_seal  # noqa: E402
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


def _reasons_text(verdict):
    reasons = verdict.get("incompleteReasons") or []
    return " incompleteReasons=" + json.dumps(reasons) if reasons else ""


def _policy(cfg, mode, bars_spec):
    """The caller's letterbox decision for ONE capture (see look_metrics.make_letterbox_policy)."""
    import look_metrics

    declared = look_metrics.parse_declared_bars(bars_spec) if bars_spec else None
    return look_metrics.make_letterbox_policy(cfg, mode=mode, declared=declared)


def _refuse_same_directory(first, second, what):
    """Comparing a directory with itself is perfect agreement by construction, so it is refused, not measured."""
    if look_seal.canon_path(first) == look_seal.canon_path(second):
        raise ValueError(f"{what}: both sides are the same directory ({first!r}); a frame set always agrees with itself")


def cmd_floor(args):
    import look_metrics

    cfg, meta = look_config.load_config(args.config)
    policy = _policy(cfg, args.letterbox, args.letterbox_bars)
    # "Given" is `is not None`, never truthiness: `--baseline-dir ""` (an unset variable in a wrapper script) is a
    # baseline that was REQUESTED and is not there, so it is an error, not "no baseline asked for".
    requested = args.baseline_dir is not None
    if requested:
        _refuse_same_directory(args.frames_dir, args.baseline_dir, "floor --baseline-dir")
    frames = look_metrics.index_frames(args.frames_dir)
    # A baseline dir that was GIVEN is a baseline that was REQUESTED: whatever it lacks is missing, not "not asked for".
    baselines = look_metrics.index_frames(args.baseline_dir) if requested else {}
    verdict = look_metrics.evaluate_sheet(frames, cfg, meta, label=args.label, references=baselines,
                                          letterbox_policy=policy, baseline_requested=requested)
    verdict["createdUtc"] = _now()
    verdict["source"] = args.source_note
    _write_json(args.out, verdict)
    skin = verdict["skinCheck"]
    print(f"floor {args.label}: {verdict['outcome']} frames={verdict['frameCount']} counts={verdict['counts']} "
          f"skinCheck={skin['status']} applied={skin['appliedToFrames']}/{skin['ofFrames']}"
          + _reasons_text(verdict))
    return _exit_for(verdict["outcome"])


def cmd_pair_metrics(args):
    import look_metrics

    cfg, meta = look_config.load_config(args.config)
    overrides = _apply_overrides(cfg, args)
    _refuse_same_directory(args.a_dir, args.b_dir, "pair-metrics")
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
          f"meanSsim={agg.get('meanSsimLuma')} meanMismatch={agg.get('meanMismatchFraction')}"
          + _reasons_text(verdict))
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

    prepared, records = {}, []
    os.makedirs(out_dir, exist_ok=True)
    index = look_metrics.index_frames(directory)  # all or nothing: a frame the judges would never see refuses the directory
    for number, path in index.items():
        arr = look_metrics.load_rgb(path)
        letterbox = look_metrics.detect_letterbox(arr, cfg, policy)
        active = look_metrics.crop_active(arr, letterbox)
        target = os.path.join(out_dir, f"{name}-{number:02d}.png")
        Image.fromarray(np.ascontiguousarray(active), "RGB").save(target)
        prepared[number] = target
        records.append({
            "subject": name, "frameId": number, "sourceSize": [int(arr.shape[1]), int(arr.shape[0])],
            "preparedSize": [int(active.shape[1]), int(active.shape[0])],
            "letterbox": {k: letterbox[k] for k in ("mode", "provenance", "top", "bottom", "left", "right",
                                                    "excludedPct", "refused", "notes", "candidate", "declared")}})
    return prepared, records


def _common_crop(a_frames, b_frames, tolerance_px, name_a="a", name_b="b"):
    """Centre-crop each shared frame pair to a common size when the sizes differ by <= tolerance. Returns
    (kept indices, dropped, crops) where dropped = [{"frameId", "reason"}] (a frame that is not kept is never
    silent) and crops = [{"frameId", "from": {name: [w, h]}, "to": [w, h]}] for every pair that was cropped."""
    import numpy as np
    from PIL import Image

    keep, dropped, crops = [], [], []
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
            crops.append({"frameId": index, "from": {name_a: [wa, ha], name_b: [wb, hb]}, "to": [w, h]})
        keep.append(index)
    return keep, dropped, crops


def cmd_build_session(args):
    cfg, meta = look_config.load_config(args.config)
    lock = look_config.verify_rubric_lock()  # the digest is recorded BEFORE any image exists
    allowance = args.allow_dropped_frames
    if allowance is not None and not allowance.strip():
        raise ValueError("--allow-dropped-frames needs a reason (say why losing those frames is acceptable)")
    name_a, dir_a = _parse_subject(args.a)
    name_b, dir_b = _parse_subject(args.b)
    _refuse_same_directory(dir_a, dir_b, "build-session")  # one directory under two names: every real pair is identical
    key_hex = None
    if not args.no_seal:
        if not args.seal_key_file or not str(args.seal_key_file).strip():
            raise ValueError("build-session seals the session and needs --seal-key-file PATH (outside the session "
                             "directory) to keep the key in; the key is never printed. Or pass --no-seal (debugging only).")
        _refuse_key_file_in_session(args.seal_key_file, args.out_dir)
        if os.path.exists(args.seal_key_file):  # checked up front so a long build is not wasted on it
            raise ValueError(f"--seal-key-file {args.seal_key_file!r} already exists: it may hold another session's "
                             "only key; name a new file")
        key_hex = look_seal.new_key()
    prep = os.path.join(args.out_dir, "source-frames")
    policy_a = _policy(cfg, args.letterbox, args.a_letterbox_bars)
    policy_b = _policy(cfg, args.letterbox, args.b_letterbox_bars)
    fa, records_a = _prepare_subject_frames(name_a, dir_a, prep, cfg, policy_a)
    fb, records_b = _prepare_subject_frames(name_b, dir_b, prep, cfg, policy_b)
    keep, dropped, common_crops = _common_crop(fa, fb, args.common_crop_tolerance_px, name_a, name_b)
    capture = {"configSha256": meta["configSha256"], "configVersion": meta["configVersion"],
               "letterboxPolicy": {name_a: policy_a, name_b: policy_b}, "frameCrops": records_a + records_b,
               "commonCrops": common_crops, "commonCropTolerancePx": args.common_crop_tolerance_px}
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
        created_utc=_now(), dropped_frames=dropped, crop_tolerance_px=args.common_crop_tolerance_px,
        capture=capture, drop_allowance=allowance)
    dropped_sorted = sorted(dropped, key=lambda d: d["frameId"])
    if dropped_sorted:
        print(f"[look_cli] WARNING {len(dropped_sorted)} frame(s) were NOT judged: "
              + "; ".join(f"{d['frameId']} ({d['reason']})" for d in dropped_sorted), file=sys.stderr)
    out = {"frames": keep, "droppedFrames": dropped_sorted, "files": {k: os.path.basename(v) for k, v in paths.items()}}
    if key_hex is None:
        print("[look_cli] WARNING --no-seal: the answer key and the source frames are in the session directory in "
              "the clear. A real judge refuses this session and a tally of it is unusable (SEAL_NOT_VERIFIED).",
              file=sys.stderr)
        out["sealed"] = False
    else:
        _write_key_file(args.seal_key_file, key_hex)  # BEFORE sealing: a sealed session whose key was lost is useless
        try:
            sealed = look_seal.seal_session(args.out_dir, key_hex)
        except BaseException:
            try:
                os.remove(args.seal_key_file)  # nothing was sealed with this key, so it must not linger
            except OSError:
                pass
            raise
        out.update({"sealed": True, "sealSha256": sealed["sealSha256"], "sealedMembers": len(sealed["members"])})
        print(f"[look_cli] SEALED: the answer key and source frames are in {look_seal.SEALED_NAME}, readable only with "
              "the key in the file you named with --seal-key-file. Keep that file OUT of the judge's reach (the judge "
              f"refuses {look_seal.KEY_ENV} in its environment); give it to `tally` with --seal-key-file (or "
              f"{look_seal.KEY_ENV}).", file=sys.stderr)
    print(json.dumps(out))
    lost = look_pairs.drop_policy(dropped)["lossFrameIds"]
    if lost and allowance is None:
        print(f"[look_cli] ERROR UNACKNOWLEDGED_DROPPED_FRAMES: frames {lost} were lost, not chosen. The session is "
              "written and marked, and look_tally will call it unusable; rebuild with the frames fixed, or pass "
              "--allow-dropped-frames REASON to record why the loss is acceptable.", file=sys.stderr)
        return 2
    return 0


def _refuse_key_file_in_session(key_file, session_dir):
    """A key file inside the session directory is a key the judge can read."""
    if look_seal.path_inside(key_file, session_dir):
        raise ValueError(f"--seal-key-file {key_file!r} is inside the session directory: a judge could read it")


def _write_key_file(path, key_hex):
    """Write the seal key to `path`, never over an existing file (it could be another session's only key)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        handle = open(path, "x", encoding="utf-8", newline="\n")
    except FileExistsError as exc:
        raise ValueError(f"--seal-key-file {path!r} already exists: it may hold another session's only key; "
                         "name a new file") from exc
    with handle:
        handle.write(key_hex + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _runner_from_spec(spec, args):
    kind, _, model = spec.partition(":")
    if kind == "claude":
        return look_judges.ClaudeCliJudge(model, timeout_s=args.item_timeout_s)
    if kind == "codex":
        return look_judges.CodexExecJudge(model or None, timeout_s=args.item_timeout_s)
    raise SystemExit(f"--runner must be claude:MODEL or codex:MODEL, got {spec!r}")


def cmd_judge(args):
    look_judges.assert_no_seal_key_in_environment()  # fail before anything starts: the judge must not inherit the key
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


def _explicit_path(value, flag):
    """An explicitly GIVEN path that is empty is a request that is not there (an unset variable in a wrapper), never
    'use the shipped one'."""
    if value is not None and not str(value).strip():
        raise ValueError(f"{flag} was given empty: name the file, or omit the flag to use the shipped one")
    return value


def _verified_lock(args):
    """The rubric lock verified NOW: the shipped one, or the --rubric / --rubric-lock the caller named."""
    return look_config.verify_rubric_lock(_explicit_path(args.rubric, "--rubric"),
                                          _explicit_path(args.rubric_lock, "--rubric-lock"))


def _load_secrets(args):
    """(answer_key, full session, seal dict-or-None). A sealed session is opened from memory with the seal key (the
    seal must verify, or this raises); the seal facts come from sealed.bin and the directory, never from a results file.
    An UNSEALED session is read as plaintext so the diagnosis can still be written, but it carries no seal: the entry is
    unusable (SEAL_NOT_VERIFIED), whatever the verdicts say."""
    sealed_file = os.path.isfile(look_seal.sealed_path(args.session_dir))
    key_hex = look_seal.key_from_args(args.seal_key, args.seal_key_file)
    if sealed_file:
        if not key_hex:
            raise ValueError(f"{args.session_dir!r} is sealed: pass the key in build-session's --seal-key-file "
                             f"(--seal-key, --seal-key-file or {look_seal.KEY_ENV})")
        key, session, seal_sha = look_seal.open_sealed(args.session_dir, key_hex)
        return key, session, {"verified": True, "sealSha256": seal_sha,
                              "plaintextPresent": look_seal.session_state(args.session_dir)["plaintextPresent"]}
    return (_read_json(os.path.join(args.session_dir, "answer_key.json")),
            _read_json(os.path.join(args.session_dir, "session.json")), None)


def _read_results(path):
    """A judge results file as run_session writes it (v2), or a typed refusal: the keys the tally reads must be there."""
    results = _read_json(path)
    if not (isinstance(results, dict) and results.get("schema") == look_judges.RESULTS_SCHEMA
            and isinstance(results.get("items"), dict) and isinstance(results.get("runner"), dict)):
        raise ValueError(f"{path!r} is not a {look_judges.RESULTS_SCHEMA} judge results file (schema, items and runner "
                         "are required): re-run `judge` with this version")
    return results


def cmd_tally(args):
    key, session, seal = _load_secrets(args)
    results = _read_results(args.results)
    cfg, meta = look_config.load_config(args.config)
    lock = _verified_lock(args)  # the CURRENT lock, or the one named explicitly
    canary = _read_json(_explicit_path(args.canary, "--canary")) if args.canary is not None else None
    entry = look_tally.tally(
        key, results["items"], results["runner"], session, cfg,
        current_image_sha256=_current_image_digests(args.session_dir), forbidden_models=args.producer_model,
        config_sha256=meta["configSha256"], rubric_lock=lock, seal=seal, canary=canary)
    entry["errorsDuringJudging"] = results.get("errors", {})
    entry["staleRejectedAtJudging"] = results.get("staleRejected", {})
    entry["createdUtc"] = _now()
    _write_json(args.out, entry)
    # The console line is a view of the entry AS WRITTEN. It is read back from the file on purpose: the in-memory entry
    # was computed from the unsealed answer key, and nothing derived from that key is ever logged directly.
    written = _read_json(args.out)
    print(f"tally {written['judgeId']}: winner={written['preference']['winner']} usable={written['usable']} "
          f"consistentUnits={written['preference']['consistentUnits']} flips={written['preference']['discardedFlips']} "
          f"control={written['controlResult']['outcome']} positiveControl={written['positiveControlResult']['outcome']} "
          f"slotBias={written['slotTally']['slotBias']} unusableReasons={written['unusableReasons']}")
    return 0 if written["usable"] else 2


def cmd_judge_disagreement(args):
    cfg, _ = look_config.load_config(args.config)
    entries = [_read_json(path) for path in args.entries]
    lock = _verified_lock(args)  # the CURRENT lock, or the one named explicitly
    verdict = look_tally.judge_disagreement(entries, cfg["judge_disagreement"]["third_judge_points"], rubric_lock=lock,
                                            require_cross_family=args.require_cross_family)
    verdict["createdUtc"] = _now()
    if args.out:
        _write_json(args.out, verdict)
    print(json.dumps({k: verdict[k] for k in ("thirdJudgeNeeded", "thresholdPoints", "comparable", "unusableJudges",
                                              "sessionMismatches", "identityProblems", "coverageProblems",
                                              "rubricLockProblems")}))
    return 0 if verdict["comparable"] else 2


def cmd_unseal(args):
    """Audit only: write the sealed members (answer key, full session, source frames) under --out-dir, which may not be
    inside the session directory. Needs the seal key."""
    key_hex = look_seal.key_from_args(args.seal_key, args.seal_key_file)
    if not key_hex:
        raise ValueError("unseal needs the seal key")
    look_seal.extract_all(args.session_dir, key_hex, args.out_dir)
    print(json.dumps({"extractedTo": args.out_dir}))
    return 0


def cmd_isolation_canary(args):
    import look_canary

    report = look_canary.run_canary(args.runner, timeout_s=args.timeout_s, model=args.model, base_dir=args.canary_dir)
    report["createdUtc"] = _now()
    if args.out:
        _write_json(args.out, report)
    print(json.dumps({k: report.get(k) for k in ("runner", "outcome", "leaked", "judgeTried", "controlRead", "cliVersion")}))
    return 0 if report["outcome"] == look_canary.HELD else 2


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
    s.add_argument("--allow-dropped-frames", metavar="REASON",
                   help="acknowledge that frames were LOST (unshared, outside the crop tolerance, requested but absent) "
                        "and why that is acceptable; without it the session is marked and the tally calls it unusable")
    s.add_argument("--seal-key-file", help="where to write the new seal key (a file that does not exist yet, outside the "
                                           "session dir). Required unless --no-seal: the key is never printed")
    s.add_argument("--no-seal", action="store_true",
                   help="leave the answer key and source frames in the clear (debugging only: a real judge refuses "
                        "the session and its tally is unusable)")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_build_session)

    s = sub.add_parser("judge")
    s.add_argument("--session-dir", required=True)
    s.add_argument("--runner", required=True)
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
    s.add_argument("--canary", help="the isolation-canary report (look_cli isolation-canary --out) for the runner class, CLI "
                                    "version and command this run used; without one, or with one for another version, the "
                                    "entry is unusable")
    s.add_argument("--seal-key", help="the seal key itself (prefer --seal-key-file or env LOOK_SEAL_KEY: argv is visible "
                                      "in process lists and logs)")
    s.add_argument("--seal-key-file")
    s.add_argument("--rubric", help="rubric file to verify against --rubric-lock (default: the shipped rubric)")
    s.add_argument("--rubric-lock", help="rubric lock to verify against (default: the shipped lock); the session's "
                                         "recorded rubric digest must equal it")
    s.add_argument("--producer-model", action="append", default=[], required=True,
                   help="model id of the look's producer or the hub; repeatable; the tally re-applies the guard")
    s.set_defaults(fn=cmd_tally)

    s = sub.add_parser("judge-disagreement")
    s.add_argument("--entries", nargs="+", required=True, help="tally entry JSON files, one per judge")
    s.add_argument("--out")
    s.add_argument("--config")
    s.add_argument("--rubric")
    s.add_argument("--rubric-lock")
    s.add_argument("--require-cross-family", action="store_true",
                   help="the comparison is claimed to be cross-family: the judges must be in different model families "
                        "(also assumed when an entry says CROSS_FAMILY_PROVEN_LIVE)")
    s.set_defaults(fn=cmd_judge_disagreement)

    s = sub.add_parser("unseal")
    s.add_argument("--session-dir", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--seal-key")
    s.add_argument("--seal-key-file")
    s.set_defaults(fn=cmd_unseal)

    s = sub.add_parser("isolation-canary")
    s.add_argument("--runner", required=True, choices=["claude", "codex"])
    s.add_argument("--model")
    s.add_argument("--out")
    s.add_argument("--canary-dir", help="where to make the decoys and the sealed session (default: the temp dir)")
    s.add_argument("--timeout-s", type=int, default=230)
    s.set_defaults(fn=cmd_isolation_canary)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (look_config.ConfigError, look_config.RubricLockError, look_judges.ProducerJudgeError,
            look_judges.JudgeError, ValueError) as exc:
        print(f"[look_cli] ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
