"""Verify and atomically install allowlisted vendored native payload members.

Only members named by ``tools/gates/vendored-native-payloads.json`` can be
installed.  The archive and selected member bytes, executable kind, and
architecture are revalidated immediately before installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .vendored_native_payloads import (
    DEFAULT_MANIFEST,
    ArchiveView,
    PayloadIntegrityError,
    _binary_identity,
    _hex_digest,
    _manifest_path,
    _positive_int,
    _parse_manifest_bytes,
    _require,
    _safe_member_name,
    _sha256_file,
    _sha256_stream,
    _validate_archive,
    _validate_policy,
    _validate_provenance,
    validate_manifest,
)


def load_record_only_snapshot(
    repo_root: Path,
    manifest_path: Path,
    payload_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Validate ONLY the named active record, never the repository-wide tracked-file census.

    EXPORT-PRORES-FFMPEG-EXE-PACKAGE-1 (adversary ae560106-A2): ``validate_manifest`` enumerates every
    Git-tracked file and opens a 512-byte prefix of each one, owner clip fixtures included, so a local
    packaging install that ran it read media and failed when an unrelated fixture was locked. This mode
    reads the manifest, its policy, and the one record's archive (size, sha256, Git tracking, member
    summary and limits) and selected members (size, sha256, binary format, machine and kind). The hosted
    workflows keep the full validation; they never pass ``--record-only``.
    """
    manifest_file = manifest_path if manifest_path.is_absolute() else repo_root / manifest_path
    try:
        raw = manifest_file.read_bytes()
    except OSError as exc:
        raise PayloadIntegrityError(f"cannot read payload manifest {manifest_file}: {exc}") from exc
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    manifest = _parse_manifest_bytes(raw, str(manifest_file))
    _require(manifest.get("schema_version") == 1, "payload manifest schema_version must be 1")
    policy = _validate_policy(manifest.get("integrity_policy"))
    record = _record_for_id(manifest, payload_id)
    _validate_archive(repo_root, record, policy, selected=True, require_tracked=True)
    _validate_provenance(record)
    return manifest, record, manifest_sha256


def load_validated_manifest_snapshot(
    repo_root: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], str]:
    """Read, hash, parse, and validate one immutable manifest byte snapshot."""
    manifest_file = manifest_path if manifest_path.is_absolute() else repo_root / manifest_path
    try:
        raw = manifest_file.read_bytes()
    except OSError as exc:
        raise PayloadIntegrityError(f"cannot read payload manifest {manifest_file}: {exc}") from exc
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    manifest = _parse_manifest_bytes(raw, str(manifest_file))
    validate_manifest(repo_root, manifest)
    return manifest, manifest_sha256


def _record_for_id(manifest: dict[str, Any], payload_id: str) -> dict[str, Any]:
    payloads = manifest.get("payloads")
    _require(isinstance(payloads, list), "payload manifest payloads must be a list")
    matches = [record for record in payloads if isinstance(record, dict) and record.get("id") == payload_id]
    _require(len(matches) == 1, f"payload id must identify exactly one active payload: {payload_id!r}")
    return matches[0]


def _validate_selected_bytes(
    handle: Any,
    selected: dict[str, Any],
    label: str,
) -> tuple[int, str, bytes]:
    expected_bytes = _positive_int(selected.get("bytes"), f"{label}.bytes")
    expected_hash = _hex_digest(selected.get("sha256"), label)
    observed_bytes, observed_hash, prefix = _sha256_stream(handle)
    _require(observed_bytes == expected_bytes,
             f"{label} size mismatch: expected {expected_bytes}, got {observed_bytes}")
    _require(observed_hash == expected_hash,
             f"{label} sha256 mismatch: expected {expected_hash}, got {observed_hash}")
    expected_binary = selected.get("binary")
    _require(isinstance(expected_binary, dict) and set(expected_binary) == {"format", "machine"},
             f"{label}.binary must contain exactly format and machine")
    observed_binary = _binary_identity(prefix)
    _require(
        {"format": observed_binary["format"], "machine": observed_binary["machine"]} == expected_binary,
        f"{label} binary identity mismatch: expected {expected_binary}, got {observed_binary}",
    )
    _require(observed_binary["kind"] == selected.get("kind"),
             f"{label} binary kind mismatch: manifest declares {selected.get('kind')!r}, "
             f"header identifies {observed_binary['kind']!r}")
    return observed_bytes, observed_hash, prefix


def verify_installed_payload(record: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    """Verify final installed files against one already-validated manifest record."""
    results: list[dict[str, Any]] = []
    selected_members = record.get("selected_members")
    _require(isinstance(selected_members, list) and selected_members,
             f"{record.get('id', 'payload')} selected_members must be a non-empty list")
    for index, selected in enumerate(selected_members):
        label = f"{record.get('id', 'payload')}.selected_members[{index}]"
        _require(isinstance(selected, dict), f"{label} must be an object")
        output_name = _safe_member_name(str(selected.get("output_name", "")), label)
        _require("/" not in output_name, f"{label}.output_name must be a basename")
        destination = output_dir / output_name
        _require(not destination.is_symlink(), f"{label} installed destination is a symlink: {destination}")
        _require(destination.is_file(), f"{label} installed destination is missing: {destination}")
        try:
            with destination.open("rb") as handle:
                observed_bytes, observed_hash, _ = _validate_selected_bytes(handle, selected, label)
        except OSError as exc:
            raise PayloadIntegrityError(f"{label} cannot read installed destination {destination}: {exc}") from exc
        results.append({"path": str(destination), "bytes": observed_bytes, "sha256": observed_hash})
    return results


def _publish_staged_transaction(
    staged: list[tuple[Path, Path, dict[str, Any]]],
    record: dict[str, Any],
    output_dir: Path,
    *,
    verify_installed: bool,
) -> list[dict[str, Any]]:
    """Publish every staged file or restore every original destination."""
    label = str(record.get("id") or "payload")
    try:
        transaction_dir = Path(tempfile.mkdtemp(prefix=".vendored-payload-transaction-", dir=output_dir))
    except OSError as exc:
        raise PayloadIntegrityError(f"{label} cannot create publication transaction: {exc}") from exc

    backups: dict[Path, Path | None] = {}
    published: set[Path] = set()
    try:
        for _, destination, _ in staged:
            _require(not destination.is_symlink(), f"{label} destination became a symlink: {destination}")
            _require(not destination.exists() or destination.is_file(),
                     f"{label} destination became a non-file collision: {destination}")
            backup: Path | None = None
            if destination.exists():
                backup = transaction_dir / destination.name
                os.replace(destination, backup)
            backups[destination] = backup

        for temporary, destination, _ in staged:
            if backups[destination] is None:
                _require(not destination.exists() and not destination.is_symlink(),
                         f"{label} destination appeared during publication: {destination}")
            os.replace(temporary, destination)
            published.add(destination)

        installed = verify_installed_payload(record, output_dir) if verify_installed else []
    except (OSError, PayloadIntegrityError) as exc:
        rollback_errors: list[str] = []
        for _, destination, _ in reversed(staged):
            try:
                if destination in published:
                    _require(not destination.is_symlink(),
                             f"published destination became a symlink during rollback: {destination}")
                    destination.unlink(missing_ok=True)
                backup = backups.get(destination)
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
            except (OSError, PayloadIntegrityError) as rollback_exc:
                rollback_errors.append(f"{destination}: {rollback_exc}")
        if not rollback_errors:
            try:
                transaction_dir.rmdir()
            except OSError as rollback_exc:
                rollback_errors.append(f"transaction cleanup: {rollback_exc}")
        detail = f"; rollback incomplete ({'; '.join(rollback_errors)})" if rollback_errors else "; all originals restored"
        raise PayloadIntegrityError(f"{label} publication transaction failed: {exc}{detail}") from exc

    cleanup_errors: list[str] = []
    for backup in backups.values():
        if backup is None:
            continue
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"{backup}: {exc}")
    try:
        transaction_dir.rmdir()
    except OSError as exc:
        cleanup_errors.append(f"{transaction_dir}: {exc}")
    _require(not cleanup_errors,
             f"{label} publication succeeded but transaction cleanup failed: {'; '.join(cleanup_errors)}")
    return installed


def extract_payload_record(
    repo_root: Path,
    record: dict[str, Any],
    output_dir: Path,
    *,
    archive_reference: str,
    verify_installed: bool = False,
) -> dict[str, Any]:
    """Revalidate, stage, and atomically replace all selected outputs."""
    label = str(record.get("id") or "payload")
    relative = _manifest_path(record.get("path"), label)
    _require(PurePosixPath(relative).name == archive_reference,
             f"{label} archive reference mismatch: expected {PurePosixPath(relative).name!r}, "
             f"got {archive_reference!r}")
    archive_path = repo_root / Path(relative)
    _require(archive_path.is_file(), f"{label} archive is missing: {relative}")
    expected_archive_bytes = _positive_int(record.get("bytes"), f"{label}.bytes")
    expected_archive_hash = _hex_digest(record.get("sha256"), label)
    archive_bytes, archive_hash = _sha256_file(archive_path)
    _require(archive_bytes == expected_archive_bytes,
             f"{label} archive size mismatch: expected {expected_archive_bytes}, got {archive_bytes}")
    _require(archive_hash == expected_archive_hash,
             f"{label} archive sha256 mismatch: expected {expected_archive_hash}, got {archive_hash}")

    _require(not output_dir.is_symlink(), f"{label} output directory cannot be a symlink: {output_dir}")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PayloadIntegrityError(f"{label} cannot create output directory {output_dir}: {exc}") from exc
    _require(output_dir.is_dir(), f"{label} output path is not a directory: {output_dir}")

    selected_members = record.get("selected_members")
    _require(isinstance(selected_members, list) and selected_members,
             f"{label} selected_members must be a non-empty list")
    output_names: set[str] = set()
    staged: list[tuple[Path, Path, dict[str, Any]]] = []
    archive_format = str(record.get("archive_format") or ("tar.xz" if relative.endswith(".tar.xz") else "zip"))
    try:
        with ArchiveView(archive_path, archive_format, label) as archive:
            for index, selected in enumerate(selected_members):
                temporary: Path | None = None
                item_label = f"{label}.selected_members[{index}]"
                _require(isinstance(selected, dict), f"{item_label} must be an object")
                member_name = _manifest_path(selected.get("path"), item_label)
                output_name = _safe_member_name(str(selected.get("output_name", "")), item_label)
                _require("/" not in output_name, f"{item_label}.output_name must be a basename")
                folded = output_name.casefold()
                _require(folded not in output_names, f"{label} output_name collision: {output_name!r}")
                output_names.add(folded)
                member = archive.by_name.get(member_name)
                _require(member is not None, f"{label} selected member is missing: {member_name!r}")
                _require(member.kind == "file", f"{label} selected member is not a regular file: {member_name!r}")
                destination = output_dir / output_name
                _require(not destination.is_symlink(), f"{item_label} destination is a symlink: {destination}")
                _require(not destination.exists() or destination.is_file(),
                         f"{item_label} destination collision is not a regular file: {destination}")
                try:
                    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_name}.", suffix=".tmp", dir=output_dir)
                    temporary = Path(temporary_name)
                    with os.fdopen(descriptor, "wb") as target, archive.open_member(member) as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    with temporary.open("rb") as staged_handle:
                        _validate_selected_bytes(staged_handle, selected, item_label)
                    if selected.get("executable") is True:
                        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    staged.append((temporary, destination, selected))
                except PayloadIntegrityError:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
                    raise
                except OSError as exc:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
                    raise PayloadIntegrityError(f"{item_label} cannot stage selected member: {exc}") from exc

        installed = _publish_staged_transaction(
            staged,
            record,
            output_dir,
            verify_installed=verify_installed,
        )
    finally:
        for temporary, _, _ in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "payload_id": label,
        "archive": relative,
        "archive_bytes": archive_bytes,
        "archive_sha256": archive_hash,
        "installed": installed,
    }


def extract_payload(
    repo_root: Path,
    manifest_path: Path,
    payload_id: str,
    output_dir: Path,
    *,
    archive_reference: str,
    verify_installed: bool = False,
    record_only: bool = False,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    if record_only:
        _, record, manifest_sha256 = load_record_only_snapshot(repo_root, manifest_path, payload_id)
    else:
        manifest, manifest_sha256 = load_validated_manifest_snapshot(repo_root, manifest_path)
        record = _record_for_id(manifest, payload_id)
    result = extract_payload_record(
        repo_root,
        record,
        output_dir,
        archive_reference=archive_reference,
        verify_installed=verify_installed,
    )
    result["manifest_sha256"] = manifest_sha256
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--payload-id", required=True)
    parser.add_argument("--archive-reference", required=True,
                        help="expected archive basename, binding the caller visibly to the selected archive")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify-installed", action="store_true",
                        help="rehash and re-identify the installed files after atomic replacement")
    parser.add_argument("--record-only", action="store_true",
                        help="validate only the named payload record (archive and member bytes, binary identity), "
                             "never the repository-wide tracked-file census; for local packaging installs")
    args = parser.parse_args(argv)
    try:
        result = extract_payload(
            args.repo_root,
            args.manifest,
            args.payload_id,
            args.output_dir,
            archive_reference=args.archive_reference,
            verify_installed=args.verify_installed,
            record_only=args.record_only,
        )
    except (OSError, PayloadIntegrityError) as exc:
        print(f"vendored native payload extraction: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
