#!/usr/bin/env python3
"""Resolve an owner-consented clip id to its verified parts, by id only -- ATTR3-FOOTAGE-BIND-1.

This is PR-A of a three-PR chain.  It answers exactly one question, safely: for a clip id, does
the frozen spec (``tools/gates/output-budget.json`` at a pinned ref) agree with the hook's frozen
consent table (``OWNER_CONSENTED_FOOTAGE`` in ``tools/hooks/mlv-never-authorized.py``) on every
part's length, content sha256 and the sha256 of the hook's own ``norm()`` of the part's path?  It
never opens, hashes or reads footage bytes itself -- that is the presence job's job, on a
different host.

THE SPEC IS READ FROM A GIT REF, NEVER THE WORKING TREE, and never a short ref name: the CLI
offers no way to name a ref at all -- it always uses the pinned default full ref
``refs/remotes/fork/master``, so the bytes resolved are the reviewed, merged bytes, not whatever a
worktree happens to hold. ``resolve()`` still accepts a ``ref`` parameter for tests, but every
value -- default or test-supplied -- is validated as a full ``refs/remotes/...`` ref before use
(see ``_validate_full_ref``); a short ref, a range, or any other git revision selector is refused.

NO PATH IS EVER PRINTED.  ``resolve()`` returns real paths (parts carry them, and
``--emit-json`` writes them to a file for a generator to consume), but the CLI's own stdout in
``--clip-id`` summary mode reports each part by index, length and a 12-character sha256 prefix
only -- exactly the shape ``verify_consented_footage.py``'s CLI already uses for the same reason.

Exit codes: 0 ok; 1 refused (any typed ``ResolveError``); 2 usage error.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading

_GATES_DIR = os.path.dirname(os.path.abspath(__file__))
if _GATES_DIR not in sys.path:
    sys.path.insert(0, _GATES_DIR)

from verify_consented_footage import load_consent_table  # noqa: E402

HOOK_PATH = os.path.join(os.path.dirname(_GATES_DIR), "hooks", "mlv-never-authorized.py")

DEFAULT_REF = "refs/remotes/fork/master"
SPEC_PATH_IN_REPO = "tools/gates/output-budget.json"
SPEC_CAP_BYTES = 4 * 1024 * 1024

PASS = "PASS"
LENGTH_MISMATCH = "LENGTH_MISMATCH"
SHA256_MISMATCH = "SHA256_MISMATCH"
PATH_NORM_MISMATCH = "PATH_NORM_MISMATCH"

# A full ref only: refs/remotes/<remote>/<rest>, no ``..`` range syntax and -- because none of
# ``:``, ``@{``, ``^`` or ``~`` is in the allowed character class -- no path suffix, reflog
# selector, ancestry, or tilde-count selector either.  This is round 4's fix for the CLI's former
# public ``--ref`` option (now removed) and for ``resolve()``'s own ``ref`` parameter, which
# still exists for tests but is validated the same way regardless of caller.
_FULL_REF_RX = re.compile(r"^refs/remotes/[A-Za-z0-9._/-]+$")

_SHA256_HEX_RX = re.compile(r"^[0-9a-f]{64}$")

_MALFORMED = "<malformed>"


def _validate_full_ref(ref):
    """-> ``ref`` unchanged if it is a full ``refs/remotes/...`` ref; else raises ``InvalidRefError``."""
    if not isinstance(ref, str) or not _FULL_REF_RX.match(ref) or ".." in ref:
        raise InvalidRefError(None, "ref must match refs/remotes/<remote>/<branch>; no short refs, "
                                     "'..', ':', '@{', '^' or '~'")
    return ref


def _safe_summary_length(length):
    """-> ``length`` if it is a plain int (never bool); else the literal ``<malformed>``.

    Guards ``_summary_json`` against a spec whose ``length`` field is not actually a length --
    e.g. a string carrying a path -- reaching stdout verbatim.
    """
    if isinstance(length, bool) or not isinstance(length, int):
        return _MALFORMED
    return length


def _safe_summary_sha_prefix(sha256):
    """-> the first 12 characters of ``sha256`` if it is 64 lowercase hex chars; else ``<malformed>``.

    Guards ``_summary_json`` against a spec whose ``sha256`` field is not actually a sha256 --
    e.g. a string carrying a path -- having any of its characters reach stdout.
    """
    if not isinstance(sha256, str) or not _SHA256_HEX_RX.match(sha256):
        return _MALFORMED
    return sha256[:12]


class ResolveError(Exception):
    """Base of every typed refusal.  ``code`` is the machine-readable reason."""

    code = "RESOLVE_ERROR"

    def __init__(self, clip_id, detail=""):
        self.clip_id = clip_id
        self.detail = detail
        super().__init__(("%s: clip_id=%r %s" % (self.code, clip_id, detail)).strip())


class UnknownClipError(ResolveError):
    """The id is not present in the spec's ``clips[]`` at all."""

    code = "UNKNOWN_ID"


class NotConsentedError(ResolveError):
    """The id is in the spec but absent from the hook's frozen consent table."""

    code = "NOT_CONSENTED"


class ZeroPartsError(ResolveError):
    """The spec names the clip but lists zero parts for it (e.g. M02-1344)."""

    code = "ZERO_PARTS"


class PartCountMismatchError(ResolveError):
    """The spec's part count for this id does not equal the consent table's."""

    code = "PART_COUNT_MISMATCH"

    def __init__(self, clip_id, spec_count, table_count):
        self.spec_count = spec_count
        self.table_count = table_count
        super().__init__(clip_id, "spec=%d table=%d" % (spec_count, table_count))


class PartMismatchError(ResolveError):
    """At least one part failed length, sha256 or path-norm cross-check against the table."""

    code = "PART_MISMATCH"

    def __init__(self, clip_id, parts):
        self.parts = parts
        super().__init__(clip_id)


class SpecTooLargeError(ResolveError):
    """The spec exceeds the stdout cap and is refused unread."""

    code = "SPEC_TOO_LARGE"


class InvalidRefError(ResolveError):
    """``ref`` is not a full ``refs/remotes/...`` ref (see ``_validate_full_ref``)."""

    code = "INVALID_REF"


class SpecReadError(ResolveError):
    """``git show`` of the pinned ref failed."""

    code = "SPEC_READ_ERROR"


class SpecParseError(ResolveError):
    """The spec bytes are not valid UTF-8 JSON."""

    code = "SPEC_PARSE_ERROR"


class ResolvedPart(object):
    __slots__ = ("index", "path", "length", "sha256", "status")

    def __init__(self, index, path, length, sha256, status):
        self.index = index
        self.path = path
        self.length = length
        self.sha256 = sha256
        self.status = status


def _git_show_argv(repo_root, ref):
    """Pure argv builder, so a test can assert the exact command without running git."""
    return [
        "git",
        "-C",
        str(repo_root),
        "--no-replace-objects",
        "show",
        "%s:%s" % (ref, SPEC_PATH_IN_REPO),
    ]


_READ_CHUNK_BYTES = 65536


def _read_spec_bytes(repo_root, ref):
    """Stream ``git show``'s stdout in bounded chunks, refusing (and killing the child) the
    instant more than ``SPEC_CAP_BYTES`` has arrived -- never buffering an unbounded amount of
    child output in memory first and checking the length only afterwards (round 4, defect 1).
    """
    argv = _git_show_argv(repo_root, ref)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr_chunks = []

    def _drain_stderr():
        try:
            while True:
                chunk = proc.stderr.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    chunks = []
    total = 0
    oversized = False
    try:
        while True:
            chunk = proc.stdout.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > SPEC_CAP_BYTES:
                oversized = True
                break
            chunks.append(chunk)
    finally:
        if oversized:
            proc.kill()
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()
        stderr_thread.join(timeout=5)

    if oversized:
        raise SpecTooLargeError(None, "exceeds cap %d bytes" % SPEC_CAP_BYTES)
    if proc.returncode != 0:
        raise SpecReadError(None, b"".join(stderr_chunks).decode("utf-8", "replace").strip())
    return b"".join(chunks)


def _load_spec(repo_root, ref, spec_bytes):
    raw = _read_spec_bytes(repo_root, ref) if spec_bytes is None else spec_bytes
    if len(raw) > SPEC_CAP_BYTES:
        raise SpecTooLargeError(None, "%d bytes exceeds cap %d" % (len(raw), SPEC_CAP_BYTES))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecParseError(None, str(exc)) from exc


def _find_clip(spec, clip_id):
    for clip in spec.get("clips", []):
        if clip.get("id") == clip_id:
            return clip
    raise UnknownClipError(clip_id)


def _load_norm():
    """The hook's own ``norm``, loaded by path -- never a port of it (see module docstring)."""
    spec = importlib.util.spec_from_file_location("_mlv_never_authorized_for_resolver", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.norm


def resolve(clip_id, repo_root, ref=DEFAULT_REF, spec_bytes=None, table=None):
    """-> list of ``ResolvedPart`` (path, length, sha256, in part order) when every part matches.

    Raises a typed ``ResolveError`` subclass on any refusal.  ``spec_bytes`` and ``table`` exist
    for tests only; production callers pass neither and get the pinned ref and the hook's frozen
    table.  ``ref`` -- default or caller-supplied -- is always validated as a full
    ``refs/remotes/...`` ref before use; anything else raises ``InvalidRefError``.
    """
    ref = _validate_full_ref(ref)
    spec = _load_spec(repo_root, ref, spec_bytes)
    clip = _find_clip(spec, clip_id)
    spec_parts = clip.get("parts") or []
    if not spec_parts:
        raise ZeroPartsError(clip_id)

    table = load_consent_table() if table is None else table
    table_parts = table.get("clips", {}).get(clip_id)
    if not table_parts:
        raise NotConsentedError(clip_id)

    if len(spec_parts) != len(table_parts):
        raise PartCountMismatchError(clip_id, len(spec_parts), len(table_parts))

    norm = _load_norm()
    results = []
    any_mismatch = False
    for index, (spec_part, table_part) in enumerate(zip(spec_parts, table_parts)):
        path = spec_part["path"]
        length = spec_part["length"]
        spec_sha = str(spec_part["sha256"]).lower()
        table_length, table_sha, table_path_norm_sha = table_part
        table_sha = table_sha.lower()
        table_path_norm_sha = table_path_norm_sha.lower()

        if length != table_length:
            status = LENGTH_MISMATCH
        elif spec_sha != table_sha:
            status = SHA256_MISMATCH
        elif hashlib.sha256(norm(path).encode("utf-8")).hexdigest() != table_path_norm_sha:
            status = PATH_NORM_MISMATCH
        else:
            status = PASS

        if status != PASS:
            any_mismatch = True
        results.append(ResolvedPart(index=index, path=path, length=length, sha256=spec_sha, status=status))

    if any_mismatch:
        raise PartMismatchError(clip_id, tuple(results))
    return results


def _summary_json(clip_id, status, parts):
    # ``part.length`` and ``part.sha256`` come from the SPEC -- untrusted input read from a git
    # ref -- not from the hook's frozen table.  Each is validated for type/shape immediately
    # before it can reach this JSON; a field that is not actually a length or a sha256 (e.g. a
    # string carrying a path) is reported as the literal ``<malformed>`` and none of its
    # characters ever reach output (round 4, defect 3).
    return json.dumps(
        {
            "clipId": clip_id,
            "ok": status == PASS,
            "status": status,
            "partCount": len(parts),
            "parts": [
                {
                    "index": part.index,
                    "length": _safe_summary_length(part.length),
                    "sha256_12": _safe_summary_sha_prefix(part.sha256),
                    "status": part.status,
                }
                for part in parts
            ],
        },
        sort_keys=True,
    )


def _write_emit_json(path, clip_id, parts):
    payload = {
        "clipId": clip_id,
        "parts": [
            {"index": part.index, "path": part.path, "length": part.length, "sha256": part.sha256}
            for part in parts
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


def main(argv=None):
    # No ``--ref`` option (round 4, defect 2): the CLI always resolves against the pinned
    # DEFAULT_REF.  Naming a ref was never something a caller needed to do to answer "does the
    # frozen spec agree with the hook's table for this clip id", and a public option accepting
    # an arbitrary git revision selector (a short ref, a range, a reflog entry, an ancestry
    # walk) is refused rather than offered.  ``resolve()`` keeps its ``ref`` parameter for
    # tests, validated the same way regardless of caller.
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--emit-json", default=None, help="write the full resolved parts, paths included, to FILE")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    try:
        parts = resolve(args.clip_id, args.repo_root)
    except PartMismatchError as exc:
        sys.stdout.write(_summary_json(args.clip_id, exc.code, exc.parts) + "\n")
        return 1
    except ResolveError as exc:
        sys.stdout.write(_summary_json(args.clip_id, exc.code, ()) + "\n")
        return 1

    sys.stdout.write(_summary_json(args.clip_id, PASS, parts) + "\n")
    if args.emit_json:
        _write_emit_json(args.emit_json, args.clip_id, parts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
