#!/usr/bin/env python3
"""Verify owner-consented footage CONTENT against the frozen table before a clip is opened.

NA4-OWNER-CONSENTED-FOOTAGE-1, round 6 (NARROW).  The project hook
(tools/hooks/mlv-never-authorized.py) holds the frozen consent record ``OWNER_CONSENTED_FOOTAGE``
but admits NO consented path from agent command text: such a path is denied by NA-4 exactly
as on master.  THE ROUTE THIS CARD PROVIDES for consented footage is tracked, id-addressed
consumers that verify content against that table, and this module is that check: for one
consented id it checks every part's existence, byte length and sha256 against the table,
hashing in streaming chunks.  IT IS NOT THE ONLY WAY A CONSENTED CLIP CAN BE OPENED TODAY:
NA-4 exception (2) still admits the ONE canonical path on the card's CLIP_OR_NONE line, and
nothing excludes a consented clip's own path from it, so a consented clip named there is
opened with NO call to this module, exactly as before this card.

EVERY SUCH CONSUMER MUST CALL ``verify`` (or the CLI) BEFORE OPENING A CLIP, and must not open
it unless the result is ``ok``.  As of this change no consumer is wired to it: the CUDA job is
card ATTR3-FOOTAGE-BIND-1, and the #72b delta tooling must be made tracked and id-only first.
LIMIT: this narrows scope, not exposure; an interpreter one-liner can still open any path,
because a text-matching hook never sees the path it opens.

The table is imported from the hook module itself, so there is exactly one copy of it.  The
output never echoes a part path: parts are reported by index only.

CLI:
    py -3 tools/gates/verify_consented_footage.py --clip-id <ID> --part <path> [--part <path> ...]
Prints one JSON object.  Exit 0 = every part verified; 1 = any mismatch, a missing part, an
unknown id or a wrong part count; 2 = usage error.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict, dataclass, field

HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "mlv-never-authorized.py"
)
CHUNK_BYTES = 1 << 20

PASS = "PASS"
MISSING = "MISSING"
LENGTH_MISMATCH = "LENGTH_MISMATCH"
SHA256_MISMATCH = "SHA256_MISMATCH"
UNREADABLE = "UNREADABLE"
UNKNOWN_ID = "UNKNOWN_ID"
PART_COUNT_MISMATCH = "PART_COUNT_MISMATCH"


@dataclass(frozen=True)
class PartResult:
    index: int
    status: str
    expected_length: int
    actual_length: object = None
    expected_sha256: str = ""
    actual_sha256: object = None


@dataclass(frozen=True)
class VerifyResult:
    clip_id: str
    ok: bool
    status: str
    parts: tuple = field(default_factory=tuple)

    def to_json(self):
        payload = asdict(self)
        payload["parts"] = [asdict(part) for part in self.parts]
        return payload


def load_consent_table():
    """The hook's frozen ``OWNER_CONSENTED_FOOTAGE``, loaded from the hook file by path."""
    spec = importlib.util.spec_from_file_location("_mlv_never_authorized_for_verifier", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OWNER_CONSENTED_FOOTAGE


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verify_part(index, path, expected):
    expected_length, expected_sha256 = expected[0], expected[1].lower()
    base = {"index": index, "expected_length": expected_length, "expected_sha256": expected_sha256}
    if not os.path.isfile(path):
        return PartResult(status=MISSING, **base)
    try:
        actual_length = os.path.getsize(path)
    except OSError:
        return PartResult(status=UNREADABLE, **base)
    if actual_length != expected_length:
        return PartResult(status=LENGTH_MISMATCH, actual_length=actual_length, **base)
    try:
        actual_sha256 = _sha256_file(path)
    except OSError:
        return PartResult(status=UNREADABLE, actual_length=actual_length, **base)
    status = PASS if actual_sha256 == expected_sha256 else SHA256_MISMATCH
    return PartResult(
        status=status, actual_length=actual_length, actual_sha256=actual_sha256, **base
    )


def verify(clip_id, part_paths, table=None):
    """-> VerifyResult.  ``ok`` only when the id is consented and EVERY part matches in order.

    ``table`` exists for fixture-scoped tests; production callers pass nothing and get the
    hook's frozen table.
    """
    table = load_consent_table() if table is None else table
    expected_parts = table["clips"].get(clip_id)
    if not expected_parts:
        return VerifyResult(clip_id=clip_id, ok=False, status=UNKNOWN_ID)
    part_paths = list(part_paths)
    if len(part_paths) != len(expected_parts):
        return VerifyResult(clip_id=clip_id, ok=False, status=PART_COUNT_MISMATCH)
    results = tuple(
        _verify_part(index, path, expected)
        for index, (path, expected) in enumerate(zip(part_paths, expected_parts))
    )
    failed = [part.status for part in results if part.status != PASS]
    return VerifyResult(
        clip_id=clip_id, ok=not failed, status=failed[0] if failed else PASS, parts=results
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--part", action="append", required=True, dest="parts")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    result = verify(args.clip_id, args.parts)
    sys.stdout.write(json.dumps(result.to_json(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
