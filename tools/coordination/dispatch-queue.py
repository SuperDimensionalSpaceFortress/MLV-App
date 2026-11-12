#!/usr/bin/env python3
"""Durable, self-healing dispatch transactions for the dual-lane queue.

A dispatch is executable only after its structured intent exists.  ``submit`` installs the
intent first and then reconciles queue.json.  If the process dies between those two writes,
``reconcile --apply`` completes the queue write on the next health sweep.  Ledger prose remains
the human record; it is checked as evidence but is never parsed into authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


INTENT_SCHEMA = "dual-lane-dispatch-intent.v1"
QUEUE_SCHEMA = "dual-lane-queue.v1"
CONFIG_SCHEMA = "dual-lane-dispatch-intents.v1"
REQUIRED_CARD_FIELDS = {
    "id": str,
    "title": str,
    "state": str,
    "owner": str,
    "priority": int,
    "track": str,
    "dispatchedSeq": int,
    "scope": str,
}
DISPATCH_IDENTITY_FIELDS = ("id", "dispatchedSeq")
CARD_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
SEQ_HEADING = re.compile(r"^## SEQ (?P<seq>\d+)\b", re.MULTILINE)
BOLD_BACKTICK_ID = re.compile(r"\*\*`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`\*\*")


class DispatchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispatchError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"JSON root must be an object: {path}")
    return value


def resolve_inside(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DispatchError(f"{label} escapes {root}: {relative}") from exc
    return candidate


def validate_card(card: Any) -> dict[str, Any]:
    if not isinstance(card, dict):
        raise DispatchError("intent.card must be an object")
    for name, expected_type in REQUIRED_CARD_FIELDS.items():
        value = card.get(name)
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise DispatchError(f"intent.card.{name} must be {expected_type.__name__}")
        if expected_type is str and not value.strip():
            raise DispatchError(f"intent.card.{name} must not be empty")
    if not CARD_ID.fullmatch(card["id"]):
        raise DispatchError(f"invalid card id: {card['id']}")
    if card["state"] != "dispatched":
        raise DispatchError("new dispatch cards must start in state=dispatched")
    if card["priority"] < 1:
        raise DispatchError("intent.card.priority must be positive")
    if card["dispatchedSeq"] < 1:
        raise DispatchError("intent.card.dispatchedSeq must be positive")
    return card


def validate_ledger_evidence(dual_lane: Path, source: dict[str, Any], source_id: str) -> None:
    ledger_name = source.get("ledger")
    seq = source.get("seq")
    if not isinstance(ledger_name, str) or Path(ledger_name).name != ledger_name:
        raise DispatchError("intent.source.ledger must be one filename inside the dual-lane directory")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise DispatchError("intent.source.seq must be a positive integer")
    ledger = resolve_inside(dual_lane, ledger_name, "ledger")
    try:
        text = ledger.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DispatchError(f"cannot read source ledger {ledger}: {exc}") from exc
    matches = list(SEQ_HEADING.finditer(text))
    selected = [index for index, match in enumerate(matches) if int(match.group("seq")) == seq]
    if len(selected) != 1:
        raise DispatchError(f"source ledger must contain exactly one SEQ {seq} heading; found {len(selected)}")
    index = selected[0]
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    block = text[matches[index].start() : end]
    if source_id not in block:
        raise DispatchError(f"SEQ {seq} does not name sourceDispatchId {source_id}")


def validate_artifact(coordination: Path, source: dict[str, Any]) -> None:
    artifact_name = source.get("artifact")
    expected_hash = source.get("artifactSha256")
    if (artifact_name is None) != (expected_hash is None):
        raise DispatchError("intent.source artifact and artifactSha256 must be supplied together")
    if artifact_name is None:
        return
    if not isinstance(artifact_name, str) or not isinstance(expected_hash, str):
        raise DispatchError("intent.source artifact fields must be strings")
    artifact = resolve_inside(coordination, artifact_name, "artifact")
    if not artifact.is_file():
        raise DispatchError(f"source artifact is missing: {artifact}")
    actual = sha256(artifact)
    if actual != expected_hash.upper():
        raise DispatchError(f"source artifact hash mismatch: expected={expected_hash.upper()} actual={actual}")


def validate_intent(path: Path, dual_lane: Path) -> dict[str, Any]:
    intent = read_json(path)
    if intent.get("schema") != INTENT_SCHEMA:
        raise DispatchError(f"unsupported intent schema in {path}: {intent.get('schema')!r}")
    intent_id = intent.get("intentId")
    source_id = intent.get("sourceDispatchId")
    if not isinstance(intent_id, str) or not CARD_ID.fullmatch(intent_id):
        raise DispatchError(f"invalid intentId in {path}")
    if not isinstance(source_id, str) or not CARD_ID.fullmatch(source_id):
        raise DispatchError(f"invalid sourceDispatchId in {path}")
    card = validate_card(intent.get("card"))
    if intent_id != card["id"]:
        raise DispatchError(f"intentId must equal card.id in {path}")
    source = intent.get("source")
    if not isinstance(source, dict):
        raise DispatchError(f"intent.source must be an object in {path}")
    if source.get("seq") != card["dispatchedSeq"]:
        raise DispatchError(f"source.seq must equal card.dispatchedSeq in {path}")
    validate_ledger_evidence(dual_lane, source, source_id)
    validate_artifact(dual_lane.parent, source)
    return intent


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True)


def install_intent(source_path: Path, dual_lane: Path) -> Path:
    intent = validate_intent(source_path, dual_lane)
    target_dir = dual_lane / "dispatch-intents"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{intent['intentId']}.json"
    payload = canonical_json(intent).encode("utf-8")
    if target.exists():
        if target.read_bytes() != payload:
            raise DispatchError(f"conflicting durable intent already exists: {target}")
        return target
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if target.read_bytes() != payload:
            raise DispatchError(f"intent byte verification failed: {target}")
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_intents(dual_lane: Path) -> list[dict[str, Any]]:
    intent_dir = dual_lane / "dispatch-intents"
    if not intent_dir.is_dir():
        return []
    intents = [validate_intent(path, dual_lane) for path in sorted(intent_dir.glob("*.json"))]
    ids = [intent["intentId"] for intent in intents]
    if len(ids) != len(set(ids)):
        raise DispatchError("duplicate intentId values exist")
    return intents


def same_dispatch_identity(existing: Any, dispatched: dict[str, Any]) -> bool:
    """Match the immutable dispatch event without freezing the card's later lifecycle.

    Once a card has been materialized, the hub may legitimately change its state, owner,
    priority, scope, or add bookkeeping fields.  The durable intent is a delivery receipt,
    not a perpetual byte-for-byte lock on that mutable queue record.  The globally unique id
    plus the originating Fable sequence distinguish the dispatch event while allowing normal
    lifecycle updates to remain authoritative.
    """
    if not isinstance(existing, dict):
        return False
    existing_seq = existing.get("dispatchedSeq")
    return (
        isinstance(existing.get("id"), str)
        and isinstance(existing_seq, int)
        and not isinstance(existing_seq, bool)
        and all(existing.get(field) == dispatched[field] for field in DISPATCH_IDENTITY_FIELDS)
    )


def preflight_submit(dual_lane: Path, intent: dict[str, Any]) -> None:
    """Refuse an already-conflicting card before the durable intent is installed.

    Reconciliation intentionally tolerates lifecycle changes after materialization.  At the
    submission boundary, however, an existing same-id card must still be the exact proposed
    dispatch; otherwise the caller may be trying to reuse an occupied queue id.
    """
    installed_path = dual_lane / "dispatch-intents" / f"{intent['intentId']}.json"
    if installed_path.is_file():
        if installed_path.read_bytes() != canonical_json(intent).encode("utf-8"):
            raise DispatchError(f"conflicting durable intent already exists: {installed_path}")
        # A byte-identical durable receipt makes this an idempotent retry.  Its card may
        # already have advanced through normal lifecycle bookkeeping, which reconcile must
        # preserve rather than re-evaluating as a brand-new submission.
        return
    queue_path = dual_lane / "queue.json"
    if not queue_path.is_file():
        return
    queue = read_json(queue_path)
    if queue.get("schema") != QUEUE_SCHEMA or not isinstance(queue.get("items"), list):
        raise DispatchError("queue.json has an unsupported schema or missing items array")
    card = intent["card"]
    existing = [
        item for item in queue["items"]
        if isinstance(item, dict) and item.get("id") == card["id"]
    ]
    if len(existing) > 1:
        raise DispatchError(f"queue contains duplicate target id {card['id']}")
    if existing and existing[0] != card:
        raise DispatchError(f"queue card conflicts with new durable intent: {card['id']}")


def audit_prose_dispatches(
    dual_lane: Path, queue_ids: set[str], intents: list[dict[str, Any]]
) -> list[str]:
    config_path = dual_lane / "dispatch-intents.json"
    if not config_path.is_file():
        return []
    config = read_json(config_path)
    if config.get("schema") != CONFIG_SCHEMA:
        raise DispatchError(f"unsupported dispatch-intents config schema: {config.get('schema')!r}")
    first_seq = config.get("proseAuditFromSeq")
    if isinstance(first_seq, bool) or not isinstance(first_seq, int) or first_seq < 1:
        raise DispatchError("dispatch-intents.json proseAuditFromSeq must be a positive integer")
    ledger = dual_lane / "fable.md"
    text = ledger.read_text(encoding="utf-8")
    headings = list(SEQ_HEADING.finditer(text))
    covered = queue_ids | {intent["sourceDispatchId"] for intent in intents}
    missing: set[str] = set()
    for index, heading in enumerate(headings):
        if int(heading.group("seq")) < first_seq:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        for line in text[heading.end() : end].splitlines():
            if "DISPATCHED" not in line.upper():
                continue
            # Only the card identifier in the board's declaration form counts. Free-form
            # backticks later on the same line often name dependencies, constraints, or an
            # explicitly non-card concept (PROVIDER-ACTIVATION-1 was the first live example).
            for identifier in BOLD_BACKTICK_ID.findall(line):
                if identifier not in covered:
                    missing.add(identifier)
    return sorted(missing)


def build_queue_candidate(
    queue: dict[str, Any], intents: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str], list[str]]:
    if queue.get("schema") != QUEUE_SCHEMA or not isinstance(queue.get("items"), list):
        raise DispatchError("queue.json has an unsupported schema or missing items array")
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in queue["items"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], []).append(item)
    missing: list[str] = []
    present: list[str] = []
    for intent in intents:
        card = intent["card"]
        existing = by_id.get(card["id"], [])
        if len(existing) > 1:
            raise DispatchError(f"queue contains duplicate target id {card['id']}")
        if existing:
            if not same_dispatch_identity(existing[0], card):
                raise DispatchError(f"queue card has a different dispatch identity: {card['id']}")
            present.append(card["id"])
        else:
            queue["items"].append(card)
            by_id[card["id"]] = [card]
            missing.append(card["id"])
    if missing:
        current_seq = queue.get("updatedBySeq", 0)
        if isinstance(current_seq, bool) or not isinstance(current_seq, int) or current_seq < 0:
            raise DispatchError("queue.json updatedBySeq must be a non-negative integer")
        queue["updated"] = utc_now()
        queue["updatedBySeq"] = max(
            current_seq,
            max(intent["card"]["dispatchedSeq"] for intent in intents),
        )
    return queue, missing, present


def invoke_verified_writer(writer: Path, queue_path: Path, expected: str, candidate: Path) -> None:
    command = [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(writer),
        "-Path",
        str(queue_path),
        "-ExpectedCurrentSha256",
        expected,
        "-NewContentFile",
        str(candidate),
        "-Note",
        "dispatch-intent reconciliation",
    ]
    result = subprocess.run(command, text=True, capture_output=True, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise DispatchError(f"verified queue writer failed with exit {result.returncode}: {detail}")


def reconcile(dual_lane: Path, writer: Path, apply: bool) -> dict[str, Any]:
    dual_lane = dual_lane.resolve()
    queue_path = dual_lane / "queue.json"
    intents = load_intents(dual_lane)
    if not queue_path.is_file():
        if intents:
            raise DispatchError(f"queue.json is missing while {len(intents)} durable intent(s) exist")
        return {"status": "OK", "intentCount": 0, "missing": [], "healed": [], "proseOnly": []}

    for attempt in range(3):
        queue = read_json(queue_path)
        queue_ids = {
            item.get("id") for item in queue.get("items", []) if isinstance(item, dict)
        }
        prose_only = audit_prose_dispatches(dual_lane, queue_ids, intents)
        queue, missing, present = build_queue_candidate(queue, intents)
        if not missing or not apply:
            status = "PROSE_ONLY" if prose_only else ("DRIFT" if missing else "OK")
            return {
                "status": status,
                "intentCount": len(intents),
                "missing": missing,
                "present": present,
                "healed": [],
                "proseOnly": prose_only,
            }
        expected = sha256(queue_path)
        candidate_text = canonical_json(queue)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="queue-candidate-", suffix=".json",
            dir=dual_lane, delete=False
        ) as stream:
            stream.write(candidate_text)
            candidate = Path(stream.name)
        try:
            try:
                invoke_verified_writer(writer, queue_path, expected, candidate)
            except DispatchError as exc:
                if "PRE-IMAGE MISMATCH" in str(exc) and attempt < 2:
                    continue
                raise
        finally:
            candidate.unlink(missing_ok=True)
        installed = read_json(queue_path)
        installed_by_id = {
            item.get("id"): item for item in installed.get("items", []) if isinstance(item, dict)
        }
        for intent in intents:
            if not same_dispatch_identity(
                installed_by_id.get(intent["card"]["id"]), intent["card"]
            ):
                raise DispatchError(f"post-write queue verification failed: {intent['intentId']}")
        return {
            "status": "HEALED" if not prose_only else "PROSE_ONLY",
            "intentCount": len(intents),
            "missing": [],
            "present": present,
            "healed": missing,
            "proseOnly": prose_only,
        }
    raise DispatchError("queue changed during all bounded reconciliation attempts")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("submit", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--dual-lane-dir", type=Path, required=True)
        command.add_argument(
            "--writer", type=Path,
            default=Path(__file__).with_name("write-verified-json.ps1"),
        )
        command.add_argument("--apply", action="store_true")
        command.add_argument("--json", action="store_true")
        if name == "submit":
            command.add_argument("--intent-file", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        dual_lane = args.dual_lane_dir.resolve()
        if args.command == "submit":
            intent = validate_intent(args.intent_file.resolve(), dual_lane)
            preflight_submit(dual_lane, intent)
            installed = install_intent(args.intent_file.resolve(), dual_lane)
            if not args.apply:
                raise DispatchError("submit requires --apply; the durable intent was installed for recovery")
        else:
            installed = None
        report = reconcile(dual_lane, args.writer.resolve(), args.apply)
        if installed is not None:
            report["installedIntent"] = str(installed)
        output = json.dumps(report, sort_keys=True)
        print(output if args.json else output)
        return 0 if report["status"] in {"OK", "HEALED"} else 1
    except DispatchError as exc:
        report = {"status": "ERROR", "error": str(exc)}
        print(json.dumps(report, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
