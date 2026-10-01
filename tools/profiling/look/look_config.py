#!/usr/bin/env python3
"""Config + frozen-rubric loading for the Look Assist objective floor and blind judge harness.

Pure standard library on purpose: the repo-hygiene CI image has no numpy/Pillow, so everything that can
be proven without pixels (threshold reasons, rubric digest, tally maths) lives in modules that import
neither.

TWO RULES THIS FILE ENFORCES
    1. A threshold without a reason is rejected. look_floor_config.json stores every value as
       {"value": ..., "reason": "..."}; load_config() raises ConfigError on a bare number or an empty reason.
    2. The judging rubric is hashed BEFORE any judging. rubric_digest() is the sha256 of the file's bytes
       (CRLF folded to LF); judge_rubric.lock.json records it; verify_rubric_lock() raises RubricLockError when the file
       no longer matches. .gitattributes pins both files to LF so the digest is the same on every checkout.
"""
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "look_floor_config.json")
RUBRIC_PATH = os.path.join(HERE, "judge_rubric.md")
RUBRIC_LOCK_PATH = os.path.join(HERE, "judge_rubric.lock.json")

CONFIG_SCHEMA = "mlv-app/look-floor-config/v1"
RUBRIC_LOCK_SCHEMA = "mlv-app/look-rubric-lock/v1"

# The scored criteria, in rubric order. Part of the frozen rubric's contract: look_tally validates verdicts
# against this list, and test_look_judge_harness pins that every criterion has a heading in the rubric file.
CRITERIA = ("tonal_separation", "highlight_rolloff", "skin", "colour_cast", "scene_read")
NULLABLE_CRITERIA = ("skin",)


class ConfigError(ValueError):
    pass


class RubricLockError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    """sha256 of the file with CRLF folded to LF: a Windows autocrlf checkout and a Linux one then agree, which
    is what makes a recorded digest portable. (.gitattributes also pins these files to LF.)"""
    with open(path, "rb") as handle:
        return sha256_bytes(handle.read().replace(b"\r\n", b"\n"))


def _is_leaf(node):
    return isinstance(node, dict) and "value" in node


def _walk_leaves(node, path=""):
    """Yield (dotted_path, leaf_dict) for every {value, reason} leaf under node."""
    if _is_leaf(node):
        yield path, node
        return
    if isinstance(node, dict):
        for key, child in node.items():
            if key in ("schema", "configVersion", "note"):
                continue
            yield from _walk_leaves(child, f"{path}.{key}" if path else key)


def validate_config(doc):
    """Raise ConfigError unless every threshold is {value, reason} with a non-empty reason."""
    if not isinstance(doc, dict) or doc.get("schema") != CONFIG_SCHEMA:
        raise ConfigError(f"config schema must be {CONFIG_SCHEMA!r}")
    leaves = list(_walk_leaves(doc))
    if not leaves:
        raise ConfigError("config has no thresholds")
    for path, leaf in leaves:
        reason = leaf.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(f"threshold {path!r} has no reason")
        extra = set(leaf) - {"value", "reason"}
        if extra:
            raise ConfigError(f"threshold {path!r} has unexpected keys {sorted(extra)}")
    # A bare scalar sitting where a leaf is expected is a threshold with no reason.
    for section in ("letterbox", "frame", "skin", "slot_bias", "judge_disagreement"):
        for key, child in (doc.get(section) or {}).items():
            if not _is_leaf(child):
                raise ConfigError(f"{section}.{key} must be a {{value, reason}} object, got {child!r}")
    for scope, body in (doc.get("pair", {}).get("scopes") or {}).items():
        for key, child in body.items():
            if not _is_leaf(child):
                raise ConfigError(f"pair.scopes.{scope}.{key} must be a {{value, reason}} object, got {child!r}")
    return leaves


def _flatten_values(node):
    if _is_leaf(node):
        return node["value"]
    if isinstance(node, dict):
        return {k: _flatten_values(v) for k, v in node.items() if k not in ("schema", "configVersion", "note")}
    return node


def load_config(path=None):
    """Return (values, meta). values mirrors the JSON with each leaf replaced by its value; meta carries the
    file sha256 (so a verdict binds to the exact thresholds it was judged against) and the path."""
    path = path or CONFIG_PATH
    with open(path, "rb") as handle:
        raw = handle.read().replace(b"\r\n", b"\n")
    doc = json.loads(raw.decode("utf-8"))
    validate_config(doc)
    values = _flatten_values(doc)
    values["configVersion"] = doc.get("configVersion")
    meta = {"configSha256": sha256_bytes(raw), "configPath": os.path.basename(path)}
    return values, meta


def rubric_digest(path=None):
    return sha256_file(path or RUBRIC_PATH)


def load_rubric_text(path=None):
    with open(path or RUBRIC_PATH, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_rubric_lock(rubric_path=None, lock_path=None, rubric_id="look-rubric-v1"):
    """Record the rubric digest. Run ONCE, before any judging, and commit the lock with the rubric."""
    rubric_path = rubric_path or RUBRIC_PATH
    lock = {
        "schema": RUBRIC_LOCK_SCHEMA,
        "rubricId": rubric_id,
        "rubricFile": os.path.basename(rubric_path),
        "rubricSha256": rubric_digest(rubric_path),
        "criteria": list(CRITERIA),
        "note": "sha256 over the bytes of the rubric file with CRLF folded to LF (also pinned to LF by .gitattributes). "
        "A different digest is a different rubric: bump rubricId, re-lock, and re-judge from scratch.",
    }
    with open(lock_path or RUBRIC_LOCK_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(lock, handle, indent=2)
        handle.write("\n")
    return lock


def verify_rubric_lock(rubric_path=None, lock_path=None):
    """Return the lock dict when the rubric still matches it; raise RubricLockError otherwise."""
    lock_path = lock_path or RUBRIC_LOCK_PATH
    try:
        with open(lock_path, "r", encoding="utf-8") as handle:
            lock = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RubricLockError(f"rubric lock unreadable ({lock_path}): {exc}") from exc
    if lock.get("schema") != RUBRIC_LOCK_SCHEMA:
        raise RubricLockError(f"rubric lock schema must be {RUBRIC_LOCK_SCHEMA!r}")
    actual = rubric_digest(rubric_path)
    if actual != lock.get("rubricSha256"):
        raise RubricLockError(
            f"rubric digest {actual} != locked {lock.get('rubricSha256')}: the rubric changed after it was frozen"
        )
    return lock
