"""Fail-closed integrity checks for Git-tracked native payload archives.

This module deliberately separates byte/structure integrity from redistribution
readiness.  A payload with incomplete upstream or licensing evidence may pass
the integrity gate only when the manifest records that debt explicitly; it is
never reported as redistribution-ready.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from .core import is_reparse_point
from .repository_identity import CANONICAL_REPOSITORY, alias_urls, is_repository_alias


DEFAULT_MANIFEST = Path("tools/gates/vendored-native-payloads.json")
PROMOTION_CLAIMS_PATH = Path("tools/gates/payload-provenance-promotion-claims.json")
PROMOTION_PROVIDER_AUTHORITY_STATE = "NOT_INSTALLED"
PROMOTION_RECEIPT_SCHEMA = "mlvapp.payload-provenance-promotion-receipt.v1"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
INSPECTION_PREFIX_BYTES = 4 * 1024 * 1024
EXPECTED_BINARY_KIND_POLICY = {
    "pe": "IMAGE_FILE_DLL (0x2000) set means shared-library; clear means executable.",
    "macho": "MH_EXECUTE (2) means executable; MH_DYLIB (6) means shared-library; every fat slice must agree.",
    "elf": (
        "ET_EXEC (2) means executable; ET_DYN (3) with PT_INTERP means dynamic PIE executable; "
        "ET_DYN without PT_INTERP means shared-library. Static PIE without PT_INTERP is intentionally "
        "unsupported and fails an executable kind claim."
    ),
}
RELEASE_TARGET_BINARY_POLICY = {
    "linux-x86_64": {"format": "elf", "machine": "x86_64"},
    "macos-arm64": {"format": "macho", "machine": "arm64"},
    "macos-x86_64": {"format": "macho", "machine": "x86_64"},
    "windows-x86_64": {"format": "pe", "machine": "x86_64"},
}
RELEASE_WORKFLOW_TARGETS = {
    ".github/workflows/Linux.yml": "linux-x86_64",
    ".github/workflows/macOS-Arm64.yml": "macos-arm64",
    ".github/workflows/macOS-Intel.yml": "macos-x86_64",
    ".github/workflows/Windows.yml": "windows-x86_64",
}
PROMOTION_VERIFIER_SURFACES = (
    "closeout.config.json",
    "tools/repo_hygiene/brokered_closeout.py",
    "tools/repo_hygiene/candidate_acceptance.py",
    "tools/repo_hygiene/core.py",
    "tools/repo_hygiene/vendored_native_payloads.py",
    "tools/gates/payload-provenance-promotion-receipt.schema.json",
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class PayloadIntegrityError(RuntimeError):
    """Raised when payload bytes, structure, or manifest policy do not match."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    kind: str
    size: int
    mode: int
    compressed_size: int | None
    source: Any


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PayloadIntegrityError(message)


def _sha256_stream(handle: BinaryIO) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
        if len(prefix) < INSPECTION_PREFIX_BYTES:
            prefix.extend(chunk[: INSPECTION_PREFIX_BYTES - len(prefix)])
    return total, digest.hexdigest(), bytes(prefix)


def _sha256_file(path: Path) -> tuple[int, str]:
    with path.open("rb") as handle:
        size, digest, _ = _sha256_stream(handle)
    return size, digest


def _archive_bytes_snapshot(
    repo_root: Path,
    relative: str,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
    *,
    require_tracked: bool,
) -> bytes:
    """Capture and authenticate one immutable archive image for all subsequent reads."""

    _, snapshot = _tracked_repo_file(repo_root, {
        "path": relative,
        "bytes": expected_bytes,
        "sha256": expected_sha256,
    }, f"{label} archive", require_tracked=require_tracked)
    return snapshot


def _safe_member_name(raw_name: str, label: str) -> str:
    _require(isinstance(raw_name, str) and raw_name, f"{label} member name must be non-empty text")
    _require("\x00" not in raw_name, f"{label} member contains NUL: {raw_name!r}")
    _require(all(ord(character) >= 32 for character in raw_name),
             f"{label} member contains a control character: {raw_name!r}")
    _require(unicodedata.normalize("NFC", raw_name) == raw_name,
             f"{label} member is not NFC-normalized: {raw_name!r}")
    _require("\\" not in raw_name, f"{label} member uses a backslash path: {raw_name!r}")
    _require(not raw_name.startswith(("/", "//")), f"{label} member is absolute: {raw_name!r}")
    _require(not DRIVE_PREFIX.match(raw_name), f"{label} member is drive-qualified: {raw_name!r}")
    path = PurePosixPath(raw_name)
    _require(".." not in path.parts, f"{label} member traverses a parent: {raw_name!r}")
    _require("." not in path.parts, f"{label} member contains a dot segment: {raw_name!r}")
    normalized = path.as_posix()
    _require(normalized not in {"", "."}, f"{label} member resolves to the archive root")
    _require(normalized == raw_name.rstrip("/"), f"{label} member is not normalized: {raw_name!r}")
    for part in path.parts:
        _require(":" not in part, f"{label} member uses a Windows stream/device separator: {raw_name!r}")
        _require(not part.endswith((" ", ".")),
                 f"{label} member has a Windows-ambiguous trailing character: {raw_name!r}")
        device_name = part.split(".", 1)[0].upper()
        _require(device_name not in WINDOWS_RESERVED_NAMES,
                 f"{label} member uses a Windows reserved device name: {raw_name!r}")
    return normalized


def _manifest_path(raw_path: Any, label: str) -> str:
    _require(isinstance(raw_path, str), f"{label} path must be text")
    return _safe_member_name(raw_path, label)


def _hex_digest(value: Any, label: str) -> str:
    _require(isinstance(value, str) and HEX_SHA256.fullmatch(value) is not None,
             f"{label} sha256 must be a lowercase 64-character digest")
    return value


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{label} must be an integer")
    _require(value >= (0 if allow_zero else 1), f"{label} is outside its allowed range")
    return value


def _mach_kind(file_type: int) -> str:
    kinds = {2: "executable", 6: "shared-library"}
    _require(file_type in kinds, f"unsupported Mach-O file type {file_type}")
    return kinds[file_type]


def _thin_mach_identity(prefix: bytes, offset: int = 0) -> dict[str, str]:
    _require(offset >= 0 and offset + 16 <= len(prefix),
             f"Mach-O slice header at offset {offset} is outside the inspected prefix")
    magic = prefix[offset : offset + 4]
    _require(magic in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"},
             f"unsupported Mach-O slice magic {magic.hex()} at offset {offset}")
    order = "<" if magic == b"\xcf\xfa\xed\xfe" else ">"
    cpu_type = struct.unpack_from(f"{order}I", prefix, offset + 4)[0]
    file_type = struct.unpack_from(f"{order}I", prefix, offset + 12)[0]
    machines = {0x01000007: "x86_64", 0x0100000C: "arm64"}
    _require(cpu_type in machines, f"unsupported Mach-O CPU type 0x{cpu_type:08x}")
    return {"format": "macho", "machine": machines[cpu_type], "kind": _mach_kind(file_type)}


def _fat_mach_identity(prefix: bytes) -> dict[str, str]:
    magic = prefix[:4]
    formats = {
        b"\xca\xfe\xba\xbe": (">", False),
        b"\xbe\xba\xfe\xca": ("<", False),
        b"\xca\xfe\xba\xbf": (">", True),
        b"\xbf\xba\xfe\xca": ("<", True),
    }
    _require(magic in formats, f"unsupported fat Mach-O magic {magic.hex()}")
    order, is_64 = formats[magic]
    _require(len(prefix) >= 8, "fat Mach-O header is truncated")
    slice_count = struct.unpack_from(f"{order}I", prefix, 4)[0]
    _require(1 <= slice_count <= 32, f"fat Mach-O slice count is invalid: {slice_count}")
    entry_size = 32 if is_64 else 20
    _require(8 + slice_count * entry_size <= len(prefix), "fat Mach-O architecture table is truncated")
    identities: list[dict[str, str]] = []
    for index in range(slice_count):
        entry = 8 + index * entry_size
        cpu_type = struct.unpack_from(f"{order}I", prefix, entry)[0]
        if is_64:
            slice_offset = struct.unpack_from(f"{order}Q", prefix, entry + 8)[0]
        else:
            slice_offset = struct.unpack_from(f"{order}I", prefix, entry + 8)[0]
        identity = _thin_mach_identity(prefix, slice_offset)
        expected_cpu = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(cpu_type)
        _require(expected_cpu is not None, f"unsupported fat Mach-O CPU type 0x{cpu_type:08x}")
        _require(identity["machine"] == expected_cpu,
                 f"fat Mach-O slice {index} CPU table/header mismatch")
        identities.append(identity)
    kinds = {identity["kind"] for identity in identities}
    _require(len(kinds) == 1, "fat Mach-O slices disagree on executable/shared-library kind")
    machines = "+".join(sorted({identity["machine"] for identity in identities}))
    return {"format": "macho", "machine": machines, "kind": identities[0]["kind"]}


def _elf_has_interp(prefix: bytes, elf_class: int, order: str) -> bool:
    if elf_class == 1:
        _require(len(prefix) >= 46, "ELF32 header is truncated before its program-header fields")
        program_offset = struct.unpack_from(f"{order}I", prefix, 28)[0]
        entry_size = struct.unpack_from(f"{order}H", prefix, 42)[0]
        entry_count = struct.unpack_from(f"{order}H", prefix, 44)[0]
    else:
        _require(len(prefix) >= 58, "ELF64 header is truncated before its program-header fields")
        program_offset = struct.unpack_from(f"{order}Q", prefix, 32)[0]
        entry_size = struct.unpack_from(f"{order}H", prefix, 54)[0]
        entry_count = struct.unpack_from(f"{order}H", prefix, 56)[0]
    _require(entry_count <= 1024, f"ELF program-header count is invalid: {entry_count}")
    if entry_count == 0:
        return False
    _require(entry_size >= 4, f"ELF program-header entry size is invalid: {entry_size}")
    _require(program_offset + entry_size * entry_count <= len(prefix),
             "ELF program-header table is outside the inspected prefix")
    return any(
        struct.unpack_from(f"{order}I", prefix, program_offset + index * entry_size)[0] == 3
        for index in range(entry_count)
    )


def _binary_identity(prefix: bytes) -> dict[str, str]:
    if prefix.startswith(b"MZ"):
        _require(len(prefix) >= 0x40, "PE payload header is truncated before e_lfanew")
        pe_offset = struct.unpack_from("<I", prefix, 0x3C)[0]
        _require(pe_offset + 24 <= len(prefix), "PE payload header offset is outside the inspected prefix")
        _require(prefix[pe_offset : pe_offset + 4] == b"PE\0\0", "PE payload signature is invalid")
        machine = struct.unpack_from("<H", prefix, pe_offset + 4)[0]
        characteristics = struct.unpack_from("<H", prefix, pe_offset + 22)[0]
        machines = {0x014C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}
        _require(machine in machines, f"unsupported PE machine 0x{machine:04x}")
        kind = "shared-library" if characteristics & 0x2000 else "executable"
        return {"format": "pe", "machine": machines[machine], "kind": kind}

    if prefix.startswith(b"\x7fELF"):
        _require(len(prefix) >= 20, "ELF payload header is truncated")
        elf_class = prefix[4]
        endian = prefix[5]
        _require(elf_class in {1, 2}, f"unsupported ELF class {elf_class}")
        _require(endian in {1, 2}, f"unsupported ELF endianness {endian}")
        order = "<" if endian == 1 else ">"
        elf_type = struct.unpack_from(f"{order}H", prefix, 16)[0]
        machine = struct.unpack_from(f"{order}H", prefix, 18)[0]
        machines = {3: "x86", 62: "x86_64", 183: "arm64"}
        _require(machine in machines, f"unsupported ELF machine {machine}")
        if elf_type == 2:
            kind = "executable"
        elif elf_type == 3:
            kind = "executable" if _elf_has_interp(prefix, elf_class, order) else "shared-library"
        else:
            raise PayloadIntegrityError(f"unsupported ELF e_type {elf_type}")
        return {"format": "elf", "machine": machines[machine], "kind": kind}

    if prefix[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
        return _thin_mach_identity(prefix)

    if prefix[:4] in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
        return _fat_mach_identity(prefix)

    raise PayloadIntegrityError(f"selected payload has an unrecognized executable format: {prefix[:8].hex()}")


class ArchiveView:
    def __init__(self, source: Path | bytes, archive_format: str, label: str):
        self.source = source
        self.archive_format = archive_format
        self.label = label
        self._archive: zipfile.ZipFile | tarfile.TarFile | None = None
        self._source_handle: io.BytesIO | None = None
        self.members: list[ArchiveMember] = []
        self.by_name: dict[str, ArchiveMember] = {}

    def __enter__(self) -> "ArchiveView":
        try:
            archive_source: Path | io.BytesIO
            if isinstance(self.source, bytes):
                self._source_handle = io.BytesIO(self.source)
                archive_source = self._source_handle
            else:
                archive_source = self.source
            if self.archive_format == "zip":
                archive = zipfile.ZipFile(archive_source)
                self._archive = archive
                for info in archive.infolist():
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if info.flag_bits & 0x1:
                        kind = "encrypted"
                    elif info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        kind = "unsupported-compression"
                    elif info.is_dir():
                        kind = "directory"
                    elif mode and stat.S_ISLNK(mode):
                        kind = "symlink"
                    elif mode and not stat.S_ISREG(mode):
                        kind = "special"
                    else:
                        kind = "file"
                    self.members.append(
                        ArchiveMember(info.filename, kind, info.file_size, mode, info.compress_size, info)
                    )
            elif self.archive_format == "tar.xz":
                archive = tarfile.open(
                    archive_source if isinstance(archive_source, Path) else None,
                    mode="r:xz",
                    fileobj=None if isinstance(archive_source, Path) else archive_source,
                )
                self._archive = archive
                for info in archive.getmembers():
                    if info.isfile():
                        kind = "file"
                    elif info.isdir():
                        kind = "directory"
                    elif info.issym():
                        kind = "symlink"
                    elif info.islnk():
                        kind = "hardlink"
                    else:
                        kind = "special"
                    self.members.append(ArchiveMember(info.name, kind, info.size, info.mode, None, info))
            else:
                raise PayloadIntegrityError(f"{self.label} has unsupported archive_format {self.archive_format!r}")
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise PayloadIntegrityError(f"{self.label} cannot be opened safely: {exc}") from exc

        seen: set[str] = set()
        seen_casefold: dict[str, str] = {}
        for member in self.members:
            normalized = _safe_member_name(member.name, self.label)
            _require(normalized not in seen, f"{self.label} contains duplicate member {normalized!r}")
            folded = normalized.casefold()
            _require(folded not in seen_casefold,
                     f"{self.label} contains case-colliding members {seen_casefold.get(folded)!r} and {normalized!r}")
            _require(member.kind in {"file", "directory"},
                     f"{self.label} contains forbidden {member.kind} member {normalized!r}")
            seen.add(normalized)
            seen_casefold[folded] = normalized
            self.by_name[normalized] = member
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._archive is not None:
            self._archive.close()
        if self._source_handle is not None:
            self._source_handle.close()

    def open_member(self, member: ArchiveMember) -> BinaryIO:
        _require(member.kind == "file", f"{self.label} selected member {member.name!r} is not a regular file")
        _require(self._archive is not None, "archive is not open")
        if isinstance(self._archive, zipfile.ZipFile):
            return self._archive.open(member.source, mode="r")
        handle = self._archive.extractfile(member.source)
        _require(handle is not None, f"{self.label} could not read selected member {member.name!r}")
        return handle


def _summary(members: Iterable[ArchiveMember]) -> dict[str, int]:
    rows = list(members)
    return {
        "entries": len(rows),
        "file_entries": sum(member.kind == "file" for member in rows),
        "directory_entries": sum(member.kind == "directory" for member in rows),
        "total_uncompressed_bytes": sum(member.size for member in rows if member.kind == "file"),
    }


def _validate_policy(raw: Any) -> dict[str, Any]:
    _require(isinstance(raw, dict), "integrity_policy must be an object")
    suffixes = raw.get("tracked_artifact_suffixes")
    _require(isinstance(suffixes, list) and suffixes, "tracked_artifact_suffixes must be a non-empty list")
    _require(all(isinstance(item, str) and item.startswith(".") and item == item.lower() for item in suffixes),
             "tracked_artifact_suffixes must be lowercase suffix strings")
    _require(len(suffixes) == len(set(suffixes)), "tracked_artifact_suffixes contains duplicates")
    for key in (
        "max_archive_bytes",
        "max_members",
        "max_single_member_bytes",
        "max_total_uncompressed_bytes",
        "max_member_expansion_ratio",
        "max_archive_expansion_ratio",
    ):
        _positive_int(raw.get(key), f"integrity_policy.{key}")
    _require(raw.get("binary_kind_policy") == EXPECTED_BINARY_KIND_POLICY,
             "integrity_policy.binary_kind_policy must exactly describe the enforced header rules")
    return raw


def _validate_summary(expected: Any, observed: dict[str, int], label: str) -> None:
    _require(isinstance(expected, dict), f"{label} member_summary must be an object")
    _require(set(expected) == set(observed), f"{label} member_summary fields are incomplete or unexpected")
    for key, value in observed.items():
        _positive_int(expected.get(key), f"{label} member_summary.{key}", allow_zero=True)
        _require(expected[key] == value,
                 f"{label} member_summary.{key} mismatch: expected {expected[key]}, got {value}")


def _unique_marker(text: str, marker: Any, label: str) -> int:
    _require(isinstance(marker, str) and marker, f"{label} marker must be non-empty text")
    count = text.count(marker)
    _require(count == 1, f"{label} marker must occur exactly once; found {count}: {marker!r}")
    return text.index(marker)


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _executable_workflow_marker(
    text: str,
    marker: Any,
    label: str,
    *,
    require_scalar_run: bool = False,
) -> int:
    """Locate one marker and prove that it belongs to a GitHub Actions run step."""
    marker_index = _unique_marker(text, marker, label)
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    marker_line = -1
    for index, line in enumerate(lines):
        offsets.append(offset)
        if offset <= marker_index < offset + len(line):
            marker_line = index
        offset += len(line)
    _require(marker_line >= 0, f"{label} marker line cannot be resolved")

    line = lines[marker_line].rstrip("\r\n")
    stripped = line.lstrip(" ")
    _require(not stripped.startswith("#"), f"{label} marker is inert workflow text, not a run command")
    indent = _line_indent(line)
    run_line = -1
    scalar_run = bool(
        re.match(r"^run\s*:\s*\S", stripped) and stripped.split(":", 1)[1].strip() == marker
    )
    if scalar_run:
        run_line = marker_line
    else:
        _require(stripped == marker, f"{label} marker must be the exact executable command line")
        for index in range(marker_line - 1, -1, -1):
            candidate = lines[index].rstrip("\r\n")
            candidate_stripped = candidate.lstrip(" ")
            if not candidate_stripped or candidate_stripped.startswith("#"):
                continue
            candidate_indent = _line_indent(candidate)
            if candidate_indent >= indent:
                continue
            if re.fullmatch(r"run\s*:\s*[|>][+-]?\s*", candidate_stripped):
                run_line = index
            break
    _require(run_line >= 0, f"{label} marker is inert workflow text, not a run command")
    _require(
        not require_scalar_run or scalar_run,
        f"{label} integrity gate must be the complete scalar run command",
    )

    run_indent = _line_indent(lines[run_line])
    step_start = -1
    for index in range(run_line - 1, -1, -1):
        candidate = lines[index].rstrip("\r\n")
        candidate_stripped = candidate.lstrip(" ")
        if not candidate_stripped or candidate_stripped.startswith("#"):
            continue
        candidate_indent = _line_indent(candidate)
        if candidate_indent >= run_indent:
            continue
        if candidate_stripped.startswith("- name:"):
            step_start = index
        break
    _require(step_start >= 0, f"{label} marker is not contained by a named executable workflow step")
    step_indent = _line_indent(lines[step_start])
    step_end = len(lines)
    for index in range(step_start + 1, len(lines)):
        candidate = lines[index].rstrip("\r\n")
        if _line_indent(candidate) == step_indent and candidate.lstrip(" ").startswith("- name:"):
            step_end = index
            break
    for candidate in lines[step_start:step_end]:
        candidate_stripped = candidate.lstrip(" ")
        control_key = candidate_stripped.split(":", 1)[0].strip().strip("\"'")
        _require(
            control_key not in {"if", "continue-on-error", "shell"},
            f"{label} marker belongs to a conditional or fail-open workflow step",
        )
    return marker_index


def _workflow_job_for_marker(text: str, marker_index: int, label: str) -> str:
    lines = text.splitlines(keepends=True)
    offset = 0
    marker_line = -1
    jobs_line = -1
    jobs_indent = -1
    for index, line in enumerate(lines):
        if line.lstrip(" ").rstrip("\r\n") == "jobs:":
            jobs_line = index
            jobs_indent = _line_indent(line)
        if offset <= marker_index < offset + len(line):
            marker_line = index
            break
        offset += len(line)
    _require(marker_line > jobs_line >= 0, f"{label} is not inside the workflow jobs map")
    job_indent = jobs_indent + 2
    for index in range(marker_line, jobs_line, -1):
        candidate = lines[index].rstrip("\r\n")
        if _line_indent(candidate) != job_indent:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+):\s*", candidate.lstrip(" "))
        if match:
            return match.group(1)
    raise PayloadIntegrityError(f"{label} is not inside a named workflow job")


def _validate_consumer_claim(
    repo_root: Path,
    payload_path: Any,
    consumer: Any,
    label: str,
) -> None:
    declared_archive = _manifest_path(payload_path, f"{label}.payload")
    archive_basename = PurePosixPath(declared_archive).name
    _require(isinstance(consumer, dict), f"{label} must be an object")
    target = consumer.get("target")
    status = consumer.get("status")
    _require(isinstance(target, str) and target, f"{label} target is missing")
    _require(isinstance(status, str) and status, f"{label} status is missing")
    consumer_path = _manifest_path(consumer.get("path"), label)
    consumer_file = repo_root / consumer_path
    _require(consumer_file.is_file(), f"{label} path is missing: {consumer_path}")
    try:
        consumer_text = consumer_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PayloadIntegrityError(f"{label} cannot read {consumer_path}: {exc}") from exc
    reference_marker = consumer.get("reference_marker")
    _require(isinstance(reference_marker, str) and reference_marker,
             f"{label}.reference_marker must be non-empty text")
    _require("\\" not in reference_marker,
             f"{label}.reference_marker must use normalized forward slashes")
    _require(
        reference_marker == archive_basename or reference_marker.endswith(f"/{archive_basename}"),
        f"{label}.reference_marker must end at the exact declared archive basename "
        f"{archive_basename!r} from {declared_archive!r}",
    )
    _unique_marker(consumer_text, reference_marker, f"{label}.reference_marker")

    documented_only = status == "documented-manual-packaging-only"
    unvalidated_local = status == "active-local-install-rule-unvalidated"
    active_release = status.startswith("active-release-")
    _require(documented_only or unvalidated_local or active_release,
             f"{label} status is not a recognized consumer contract: {status!r}")
    if documented_only:
        _require("operation_marker" not in consumer and "integrity_gate" not in consumer,
                 f"{label} documented-only consumer must not imply executable extraction coverage")
        return

    operation_marker = consumer.get("operation_marker")
    _unique_marker(consumer_text, operation_marker, f"{label}.operation_marker")
    _require(reference_marker in operation_marker,
             f"{label} operation_marker does not reference the declared archive marker")
    if unvalidated_local:
        _require("integrity_gate" not in consumer,
                 f"{label} unvalidated local rule must not imply integrity-gate coverage")
        return

    gate = consumer.get("integrity_gate")
    _require(isinstance(gate, dict), f"{label} active release consumer is missing integrity_gate")
    gate_path = _manifest_path(gate.get("path"), f"{label}.integrity_gate")
    gate_file = repo_root / gate_path
    _require(gate_file.is_file(), f"{label} integrity gate path is missing: {gate_path}")
    try:
        gate_text = gate_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PayloadIntegrityError(f"{label} cannot read integrity gate {gate_path}: {exc}") from exc
    declared_target = RELEASE_WORKFLOW_TARGETS.get(gate_path)
    _require(
        declared_target == target,
        f"{label} target {target!r} does not match canonical release workflow "
        f"{gate_path!r} target {declared_target!r}",
    )
    gate_index = _executable_workflow_marker(
        gate_text,
        gate.get("marker"),
        f"{label}.integrity_gate.marker",
        require_scalar_run=True,
    )
    boundary_marker = gate.get("must_precede")
    boundary_index = _executable_workflow_marker(
        gate_text, boundary_marker, f"{label}.integrity_gate.must_precede"
    )
    gate_job = _workflow_job_for_marker(
        gate_text, gate_index, f"{label}.integrity_gate.marker"
    )
    boundary_job = _workflow_job_for_marker(
        gate_text, boundary_index, f"{label}.integrity_gate.must_precede"
    )
    _require(
        gate_job == boundary_job,
        f"{label} integrity gate and extraction/build boundary are in different workflow jobs",
    )
    _require(gate_index < boundary_index,
             f"{label} integrity gate does not precede its extraction/build boundary")
    if gate_path == consumer_path:
        _require(boundary_marker == operation_marker,
                 f"{label} same-file gate boundary must equal the extraction operation_marker")


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _payload_promotion_identity(record: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "path",
        "archive_format",
        "bytes",
        "sha256",
        "member_summary",
        "selected_members",
        "consumers",
    )
    _require(all(field in record for field in fields),
             f"{record.get('id', 'payload')} is missing a promotion identity field")
    return {field: record[field] for field in fields}


def _manifest_promotion_binding(manifest: dict[str, Any]) -> str:
    payloads = manifest.get("payloads")
    inactive = manifest.get("inactive_artifacts")
    _require(isinstance(payloads, list), "payload manifest payloads must be a list")
    _require(isinstance(inactive, list), "payload manifest inactive_artifacts must be a list")
    identities = [_payload_promotion_identity(record) for record in payloads]
    identities.sort(key=lambda record: str(record["id"]))
    return _canonical_sha256({
        "schema_version": manifest.get("schema_version"),
        "integrity_policy": manifest.get("integrity_policy"),
        "payloads": identities,
        "inactive_artifacts": inactive,
    })


def _load_promotion_claims(repo_root: Path) -> dict[str, dict[str, Any]]:
    path = repo_root / PROMOTION_CLAIMS_PATH
    try:
        initial = path.read_bytes()
    except OSError as exc:
        raise PayloadIntegrityError(f"payload promotion claims registry is unavailable: {exc}") from exc
    specification = {
        "path": PROMOTION_CLAIMS_PATH.as_posix(),
        "bytes": len(initial),
        "sha256": hashlib.sha256(initial).hexdigest(),
    }
    _, raw = _tracked_repo_file(
        repo_root,
        specification,
        "payload promotion claims registry",
        require_tracked=False,
    )
    try:
        registry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadIntegrityError(f"payload promotion claims registry is malformed: {exc}") from exc
    _require(isinstance(registry, dict) and set(registry) == {"schema_version", "claims"},
             "payload promotion claims registry fields are incomplete or unexpected")
    _require(registry.get("schema_version") == 1,
             "payload promotion claims registry schema_version is invalid")
    rows = registry.get("claims")
    _require(isinstance(rows, list), "payload promotion claims registry claims must be a list")
    claims: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        _require(isinstance(row, dict), f"payload promotion claims[{index}] must be an object")
        kind = row.get("kind")
        _require(isinstance(kind, str) and kind,
                 f"payload promotion claims[{index}].kind must be non-empty text")
        _require(kind not in claims, f"payload promotion claims registry repeats kind {kind!r}")
        claims[kind] = row
    if claims:
        _, tracked_raw = _tracked_repo_file(
            repo_root,
            specification,
            "payload promotion claims registry",
            require_tracked=True,
        )
        _require(tracked_raw == raw, "payload promotion claims registry changed during validation")
    return claims


def _canonical_promotion_target_head(repo_root: Path) -> str:
    from .brokered_closeout import load_closeout_config
    from .candidate_acceptance import live_github_branch_head
    from .core import HygieneError

    try:
        return live_github_branch_head(
            repo_root,
            load_closeout_config(repo_root),
            CANONICAL_REPOSITORY,
            "master",
        )
    except (HygieneError, OSError, ValueError, TypeError) as exc:
        raise PayloadIntegrityError(
            f"payload promotion cannot verify the canonical GitHub target head: {exc}"
        ) from exc


def _validate_promotion_verifier_unchanged(repo_root: Path) -> None:
    """Require ready claims to run under verifier bytes already landed on target."""

    target_head = _canonical_promotion_target_head(repo_root)
    ancestry = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", target_head, "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _require(ancestry.returncode == 0,
             "payload promotion target head is not an ancestor of the candidate")
    for relative in PROMOTION_VERIFIER_SURFACES:
        current_path = repo_root / relative
        _require(current_path.is_file() and not current_path.is_symlink() and not is_reparse_point(current_path),
                 f"payload promotion verifier surface is unavailable or reparse-backed: {relative}")
        target_blob = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"{target_head}:{relative}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _require(target_blob.returncode == 0,
                 f"payload promotion verifier surface is absent from the pinned target: {relative}")
        current_blob = subprocess.run(
            ["git", "-C", str(repo_root), "hash-object", f"--path={relative}", "--", relative],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _require(current_blob.returncode == 0 and current_blob.stdout.strip() == target_blob.stdout.strip(),
                 f"payload promotion verifier surface differs from the pinned target: {relative}")


def _tracked_repo_file(
    repo_root: Path,
    specification: Any,
    label: str,
    *,
    require_tracked: bool = True,
) -> tuple[Path, bytes]:
    _require(isinstance(specification, dict), f"{label} must be an object")
    _require(set(specification) == {"path", "bytes", "sha256"},
             f"{label} must contain exactly path, bytes, and sha256")
    relative = _manifest_path(specification.get("path"), label)
    expected_bytes = _positive_int(specification.get("bytes"), f"{label}.bytes")
    expected_sha256 = _hex_digest(specification.get("sha256"), label)
    path = repo_root / Path(relative)
    _require(path.is_file() and not path.is_symlink() and not is_reparse_point(path),
             f"{label} is missing, reparse-backed, or is not a regular file: {relative}")
    root_resolved = repo_root.resolve()
    resolved = path.resolve(strict=True)
    _require(resolved.is_relative_to(root_resolved), f"{label} resolves outside the repository: {relative}")
    cursor = path
    while cursor != repo_root:
        _require(not cursor.is_symlink() and not is_reparse_point(cursor),
                 f"{label} traverses a symbolic link or reparse point: {relative}")
        cursor = cursor.parent
    if require_tracked:
        try:
            tracked = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--stage", "--", relative],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise PayloadIntegrityError(f"{label} cannot verify Git tracking: {exc}") from exc
        _require(tracked.returncode == 0, f"{label} must be Git-tracked: {relative}")
        index_rows = [row for row in tracked.stdout.splitlines() if row]
        _require(len(index_rows) == 1 and b"\t" in index_rows[0],
                 f"{label} Git index entry is ambiguous: {relative}")
        index_fields = index_rows[0].split(b"\t", 1)[0].split()
        _require(len(index_fields) == 3 and index_fields[0] in {b"100644", b"100755"} and index_fields[2] == b"0",
                 f"{label} must be a stage-0 regular file in the Git index: {relative}")
    def chain_identity() -> tuple[tuple[str, int, int, int], ...]:
        identities: list[tuple[str, int, int, int]] = []
        cursor = path
        while True:
            info = cursor.lstat()
            _require(not cursor.is_symlink() and not is_reparse_point(cursor),
                     f"{label} traverses a symbolic link or reparse point: {relative}")
            identities.append((str(cursor), int(info.st_dev), int(info.st_ino), int(info.st_mode)))
            if cursor == repo_root:
                break
            cursor = cursor.parent
        return tuple(identities)

    before_chain = chain_identity()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PayloadIntegrityError(f"{label} cannot be read: {relative}: {exc}") from exc
    try:
        opened_before = os.fstat(descriptor)
        _require(stat.S_ISREG(opened_before.st_mode),
                 f"{label} opened object is not a regular file: {relative}")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            raw = handle.read(expected_bytes + 1)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    after_chain = chain_identity()
    after_path = path.lstat()
    _require(before_chain == after_chain,
             f"{label} path identity changed while it was read: {relative}")
    _require(
        (int(opened_before.st_dev), int(opened_before.st_ino), int(opened_before.st_mode))
        == (int(after_path.st_dev), int(after_path.st_ino), int(after_path.st_mode)),
        f"{label} opened file identity differs from the validated path: {relative}",
    )
    actual_bytes = len(raw)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    _require(actual_bytes == expected_bytes,
             f"{label} size mismatch: expected {expected_bytes}, got {actual_bytes}")
    _require(actual_sha256 == expected_sha256,
             f"{label} sha256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    return path, raw


def _validate_notice_members(
    repo_root: Path,
    record: dict[str, Any],
    notices: Any,
    label: str,
) -> None:
    _require(isinstance(notices, list) and notices, f"{label} must list archive notice members")
    archive_path = repo_root / Path(_manifest_path(record.get("path"), label))
    archive_format = str(record.get("archive_format"))
    expected_bytes = _positive_int(record.get("bytes"), f"{label}.archive_bytes")
    expected_sha256 = _hex_digest(record.get("sha256"), f"{label}.archive")
    archive_snapshot = _archive_bytes_snapshot(
        repo_root,
        archive_path.relative_to(repo_root).as_posix(),
        expected_bytes,
        expected_sha256,
        label,
        require_tracked=True,
    )
    with ArchiveView(archive_snapshot, archive_format, str(record.get("id") or "payload")) as archive:
        seen: set[str] = set()
        for index, notice in enumerate(notices):
            item_label = f"{label}[{index}]"
            _require(isinstance(notice, dict), f"{item_label} must be an object")
            _require(set(notice) == {"path", "bytes", "sha256"},
                     f"{item_label} must contain exactly path, bytes, and sha256")
            member_name = _manifest_path(notice.get("path"), item_label)
            _require(member_name not in seen, f"{label} repeats archive notice {member_name!r}")
            seen.add(member_name)
            member = archive.by_name.get(member_name)
            _require(member is not None and member.kind == "file",
                     f"{item_label} is not a regular archive member: {member_name!r}")
            expected_bytes = _positive_int(notice.get("bytes"), f"{item_label}.bytes")
            expected_sha256 = _hex_digest(notice.get("sha256"), item_label)
            with archive.open_member(member) as handle:
                actual_bytes, actual_sha256, _ = _sha256_stream(handle)
            _require(actual_bytes == expected_bytes,
                     f"{item_label} size mismatch: expected {expected_bytes}, got {actual_bytes}")
            _require(actual_sha256 == expected_sha256,
                     f"{item_label} sha256 mismatch: expected {expected_sha256}, got {actual_sha256}")


def _validate_registered_promotion_claim(
    repo_root: Path,
    claims: dict[str, dict[str, Any]],
    claim_kind: Any,
    *,
    payload_id: str,
    receipt_path: str,
    receipt_sha256: str,
    payload_identity_sha256: str,
    manifest_binding_sha256: str,
) -> None:
    _require(isinstance(claim_kind, str) and claim_kind,
             f"{payload_id} promotion receipt approval_kind must be non-empty text")
    claim = claims.get(claim_kind)
    _require(claim is not None,
             f"{payload_id} promotion receipt has no provider-verified repository-owner approval claim")
    _require(isinstance(claim, dict), f"{payload_id} registered promotion claim must be an object")
    expected = {
        "schema_version": 1,
        "kind": claim_kind,
        "verdict": "APPROVE",
        "payload_id": payload_id,
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha256,
        "payload_identity_sha256": payload_identity_sha256,
        "manifest_binding_sha256": manifest_binding_sha256,
    }
    for key, value in expected.items():
        _require(claim.get(key) == value, f"{payload_id} registered promotion claim {key} mismatch")
    _require(set(claim) == set(expected) | {"reviewed_commit", "source", "scope"},
             f"{payload_id} registered promotion claim fields are incomplete or unexpected")
    _require(FULL_SHA.fullmatch(str(claim.get("reviewed_commit"))) is not None,
             f"{payload_id} registered promotion reviewed_commit is invalid")
    reviewed_commit = str(claim["reviewed_commit"])
    commit_check = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{reviewed_commit}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _require(commit_check.returncode == 0,
             f"{payload_id} registered promotion reviewed_commit is not a local commit")
    ancestry = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", reviewed_commit, "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _require(ancestry.returncode == 0,
             f"{payload_id} registered promotion reviewed_commit is not an ancestor of HEAD")
    receipt_at_review = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{reviewed_commit}:{receipt_path}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _require(receipt_at_review.returncode == 0,
             f"{payload_id} promotion receipt is absent at reviewed_commit")
    _require(hashlib.sha256(receipt_at_review.stdout).hexdigest() == receipt_sha256,
             f"{payload_id} promotion receipt at reviewed_commit does not match the approved receipt")
    manifest_at_review = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{reviewed_commit}:{DEFAULT_MANIFEST.as_posix()}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _require(manifest_at_review.returncode == 0,
             f"{payload_id} payload manifest is absent at reviewed_commit")
    reviewed_manifest = _parse_manifest_bytes(
        manifest_at_review.stdout, f"{DEFAULT_MANIFEST.as_posix()} at {reviewed_commit}"
    )
    _require(_manifest_promotion_binding(reviewed_manifest) == manifest_binding_sha256,
             f"{payload_id} reviewed manifest binding does not match the approved receipt")
    reviewed_payloads = reviewed_manifest.get("payloads")
    reviewed_records = [
        item for item in reviewed_payloads
        if isinstance(item, dict) and item.get("id") == payload_id
    ] if isinstance(reviewed_payloads, list) else []
    _require(len(reviewed_records) == 1,
             f"{payload_id} is not uniquely present in the reviewed manifest")
    _require(_canonical_sha256(_payload_promotion_identity(reviewed_records[0])) == payload_identity_sha256,
             f"{payload_id} reviewed payload identity does not match the approved receipt")
    source = claim.get("source")
    _require(isinstance(source, dict), f"{payload_id} registered promotion source must be an object")
    _require(set(source) == {
        "kind", "repository", "pull_request", "comment_id", "html_url",
        "created_at", "reviewer", "author_association", "body_sha256",
    }, f"{payload_id} registered promotion source fields are incomplete or unexpected")
    _require(source.get("kind") == "github_issue_comment",
             f"{payload_id} promotion approval source kind is invalid")
    # Recorded approvals name the repository as it was spelled when written (pre- or post-transfer).
    _require(is_repository_alias(source.get("repository")),
             f"{payload_id} promotion approval repository is invalid")
    _require(isinstance(source.get("pull_request"), int) and source["pull_request"] > 0,
             f"{payload_id} promotion approval pull_request is invalid")
    _require(isinstance(source.get("comment_id"), int) and source["comment_id"] > 0,
             f"{payload_id} promotion approval comment_id is invalid")
    expected_url = (
        f"https://github.com/{source['repository']}/pull/{source['pull_request']}"
        f"#issuecomment-{source['comment_id']}"
    )
    _require(source.get("html_url") == expected_url,
             f"{payload_id} promotion approval URL is invalid")
    _require(re.fullmatch(r"20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-6]\dZ", str(source.get("created_at"))) is not None,
             f"{payload_id} promotion approval timestamp is invalid")
    _require(isinstance(source.get("reviewer"), str) and source["reviewer"],
             f"{payload_id} promotion approval reviewer is invalid")
    _require(source.get("author_association") == "OWNER",
             f"{payload_id} promotion approval must come from a repository OWNER")
    _require(HEX_SHA256.fullmatch(str(source.get("body_sha256"))) is not None,
             f"{payload_id} promotion approval body_sha256 is invalid")
    scope = claim.get("scope")
    _require(scope == {
        "payload_redistribution": True,
        "release_publication": False,
        "provider_activation": False,
        "automatic_launch_gate": "CLOSED",
    }, f"{payload_id} promotion approval scope must preserve non-publication authority boundaries")
    _verify_provider_owner_claim(repo_root, claim, payload_id)


def _promotion_approval_body(claim: dict[str, Any]) -> str:
    """Return the exact owner-comment protocol for one immutable promotion claim."""

    return "\n".join([
        "APPROVE",
        "Kind: payload_redistribution",
        f"Payload: {claim['payload_id']}",
        f"Reviewed-Commit: {claim['reviewed_commit']}",
        f"Receipt: {claim['receipt_path']}",
        f"Receipt-SHA256: {claim['receipt_sha256']}",
        f"Payload-Identity-SHA256: {claim['payload_identity_sha256']}",
        f"Manifest-Binding-SHA256: {claim['manifest_binding_sha256']}",
        "Scope: payload_redistribution=true; release_publication=false; "
        "provider_activation=false; automatic_launch_gate=CLOSED",
    ])


def _verify_provider_owner_claim(repo_root: Path, claim: dict[str, Any], payload_id: str) -> None:
    """Revalidate the claimed owner verdict against canonical live GitHub state."""

    from .brokered_closeout import load_closeout_config
    from .candidate_acceptance import live_github_issue_comment
    from .core import HygieneError

    source = claim["source"]
    try:
        live = live_github_issue_comment(
            repo_root,
            load_closeout_config(repo_root),
            str(source["repository"]),
            int(source["comment_id"]),
        )
    except (HygieneError, OSError, ValueError, TypeError) as exc:
        raise PayloadIntegrityError(
            f"{payload_id} repository-owner approval could not be verified against canonical GitHub: {exc}"
        ) from exc
    response = live.get("response")
    _require(isinstance(response, dict),
             f"{payload_id} live promotion approval response is malformed")
    user = response.get("user")
    # Live GitHub answers under the repository's CURRENT name; the record may carry either.
    expected_issue_urls = alias_urls(
        f"https://api.github.com/repos/{{repository}}/issues/{source['pull_request']}"
    )
    expected_html_urls = alias_urls(
        f"https://github.com/{{repository}}/pull/{source['pull_request']}#issuecomment-{source['comment_id']}"
    )
    _require(response.get("id") == source["comment_id"],
             f"{payload_id} live promotion approval comment id mismatch")
    _require(response.get("html_url") in expected_html_urls,
             f"{payload_id} live promotion approval URL mismatch")
    _require(response.get("issue_url") in expected_issue_urls,
             f"{payload_id} live promotion approval pull request mismatch")
    _require(response.get("created_at") == source["created_at"],
             f"{payload_id} live promotion approval timestamp mismatch")
    _require(isinstance(user, dict) and user.get("login") == source["reviewer"],
             f"{payload_id} live promotion approval reviewer mismatch")
    _require(response.get("author_association") == "OWNER",
             f"{payload_id} live promotion approval is not authored by a repository OWNER")
    body = response.get("body")
    _require(isinstance(body, str), f"{payload_id} live promotion approval body is malformed")
    _require(hashlib.sha256(body.encode("utf-8")).hexdigest() == source["body_sha256"],
             f"{payload_id} live promotion approval body hash mismatch")
    _require(body == _promotion_approval_body(claim),
             f"{payload_id} live promotion approval body does not bind the exact promotion claim")


def _validate_promotion_receipt(
    repo_root: Path,
    manifest: dict[str, Any],
    record: dict[str, Any],
    declaration: Any,
    claims: dict[str, dict[str, Any]],
) -> None:
    payload_id = str(record.get("id") or "payload")
    _require(isinstance(declaration, dict), f"{payload_id} promotion receipt declaration must be an object")
    _require(set(declaration) == {"payload_id", "path", "sha256", "approval_kind"},
             f"{payload_id} promotion receipt declaration fields are incomplete or unexpected")
    _require(declaration.get("payload_id") == payload_id,
             f"{payload_id} promotion receipt declaration payload_id mismatch")
    receipt_path = _manifest_path(declaration.get("path"), f"{payload_id} promotion receipt")
    _require(receipt_path.startswith("tools/gates/payload-provenance/"),
             f"{payload_id} promotion receipt must live under tools/gates/payload-provenance")
    receipt_sha256 = _hex_digest(declaration.get("sha256"), f"{payload_id} promotion receipt")
    receipt_file = repo_root / Path(receipt_path)
    receipt_specification = {
        "path": receipt_path,
        "bytes": receipt_file.stat().st_size if receipt_file.is_file() else 0,
        "sha256": receipt_sha256,
    }
    _, receipt_bytes = _tracked_repo_file(
        repo_root, receipt_specification, f"{payload_id} promotion receipt"
    )
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadIntegrityError(f"{payload_id} cannot parse promotion receipt: {exc}") from exc
    _require(isinstance(receipt, dict), f"{payload_id} promotion receipt root must be an object")
    _require(set(receipt) == {"schema", "payload", "manifest", "upstream", "build", "license", "authority"},
             f"{payload_id} promotion receipt fields are incomplete or unexpected")
    _require(receipt.get("schema") == PROMOTION_RECEIPT_SCHEMA,
             f"{payload_id} promotion receipt schema is invalid")
    payload_identity_sha256 = _canonical_sha256(_payload_promotion_identity(record))
    _require(receipt.get("payload") == {
        "id": payload_id,
        "identity_sha256": payload_identity_sha256,
    }, f"{payload_id} promotion receipt payload binding mismatch")
    manifest_binding_sha256 = _manifest_promotion_binding(manifest)
    _require(receipt.get("manifest") == {"binding_sha256": manifest_binding_sha256},
             f"{payload_id} promotion receipt manifest binding mismatch")
    provenance = record.get("provenance")
    _require(isinstance(provenance, dict), f"{payload_id} provenance must be an object")

    upstream = receipt.get("upstream")
    _require(isinstance(upstream, dict) and set(upstream) == {"source", "version", "checksum_evidence"},
             f"{payload_id} promotion upstream fields are incomplete or unexpected")
    _require(upstream.get("source") == provenance.get("source"),
             f"{payload_id} promotion upstream source mismatch")
    _require(upstream.get("version") == provenance.get("version"),
             f"{payload_id} promotion upstream version mismatch")
    checksum_evidence = upstream.get("checksum_evidence")
    _require(isinstance(checksum_evidence, dict),
             f"{payload_id} promotion checksum_evidence must be an object")
    _require(set(checksum_evidence) == {"algorithm", "expected_archive_sha256", "evidence"},
             f"{payload_id} promotion checksum_evidence fields are incomplete or unexpected")
    _require(checksum_evidence.get("algorithm") == "sha256",
             f"{payload_id} promotion checksum algorithm must be sha256")
    _require(checksum_evidence.get("expected_archive_sha256") == record.get("sha256"),
             f"{payload_id} promotion checksum does not bind the tracked archive")
    checksum_file, _ = _tracked_repo_file(
        repo_root, checksum_evidence.get("evidence"), f"{payload_id} upstream checksum evidence"
    )
    _require(provenance.get("source_evidence") == checksum_file.relative_to(repo_root).as_posix(),
             f"{payload_id} provenance source_evidence does not name the bound checksum evidence")

    build = receipt.get("build")
    _require(isinstance(build, dict) and set(build) == {"basis", "evidence"},
             f"{payload_id} promotion build fields are incomplete or unexpected")
    _require(build.get("basis") in {"reproducible-recipe", "upstream-binary-provenance"},
             f"{payload_id} promotion build basis is invalid")
    build_file, _ = _tracked_repo_file(repo_root, build.get("evidence"), f"{payload_id} build evidence")
    _require(provenance.get("build_recipe") == build_file.relative_to(repo_root).as_posix(),
             f"{payload_id} provenance build_recipe does not name the bound build evidence")

    license_record = receipt.get("license")
    _require(isinstance(license_record, dict) and set(license_record) == {"expression", "evidence", "notice_members"},
             f"{payload_id} promotion license fields are incomplete or unexpected")
    _require(license_record.get("expression") == provenance.get("license"),
             f"{payload_id} promotion license expression mismatch")
    license_file, _ = _tracked_repo_file(
        repo_root, license_record.get("evidence"), f"{payload_id} license evidence"
    )
    _require(provenance.get("license_evidence") == license_file.relative_to(repo_root).as_posix(),
             f"{payload_id} provenance license_evidence does not name the bound license evidence")
    _validate_notice_members(
        repo_root,
        record,
        license_record.get("notice_members"),
        f"{payload_id} promotion license notice_members",
    )
    _require(receipt.get("authority") == {
        "repository_owner_approval_required": True,
        "grants_release_publication": False,
        "grants_provider_activation": False,
        "automatic_launch_gate": "CLOSED",
    }, f"{payload_id} promotion receipt authority boundaries are invalid")
    _validate_registered_promotion_claim(
        repo_root,
        claims,
        declaration.get("approval_kind"),
        payload_id=payload_id,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
        payload_identity_sha256=payload_identity_sha256,
        manifest_binding_sha256=manifest_binding_sha256,
    )


def _validate_readiness(repo_root: Path, manifest: dict[str, Any]) -> None:
    readiness = manifest.get("redistribution_readiness")
    payloads = manifest.get("payloads")
    _require(isinstance(payloads, list), "payload manifest payloads must be a list")
    _require(isinstance(readiness, dict), "redistribution_readiness must be an object")
    _require(set(readiness) == {"status", "enforcement", "blockers", "promotion_receipts"},
             "redistribution_readiness fields are incomplete or unexpected")
    _require(readiness.get("status") in {"blocked", "ready"}, "redistribution_readiness status is invalid")
    _require(readiness.get("enforcement") in {"advisory", "required"},
             "redistribution_readiness enforcement is invalid")
    blockers = readiness.get("blockers")
    _require(isinstance(blockers, list), "redistribution_readiness blockers must be a list")
    declarations = readiness.get("promotion_receipts")
    _require(isinstance(declarations, list), "redistribution_readiness promotion_receipts must be a list")
    claims = _load_promotion_claims(repo_root)
    if readiness["status"] == "blocked":
        _require(readiness["enforcement"] == "advisory",
                 "blocked redistribution readiness must remain advisory for this byte-integrity slice")
        _require(all(isinstance(item, str) and item for item in blockers) and blockers,
                 "blocked redistribution readiness must list explicit blockers")
        _require(all(str(record.get("redistribution_readiness", "")).startswith("blocked-") for record in payloads),
                 "every payload must remain explicitly blocked while top-level readiness is blocked")
        _require(not declarations,
                 "blocked redistribution readiness cannot carry authority-bearing promotion receipts")
        _require(not claims,
                 "blocked redistribution readiness requires an empty promotion claims registry")
        return

    _require(claims, "ready redistribution requires registered promotion claims")
    _require(
        PROMOTION_PROVIDER_AUTHORITY_STATE == "INSTALLED_TARGET_OWNED",
        "ready redistribution requires a separately activated target-owned isolated promotion verifier; "
        "payload promotion provider authority is NOT_INSTALLED",
    )
    _validate_promotion_verifier_unchanged(repo_root)
    _require(readiness["enforcement"] == "required",
             "ready redistribution status must be enforced, not advisory")
    _require(not blockers, "ready redistribution status cannot retain blockers")
    _require(len(declarations) == len(payloads),
             "ready redistribution requires exactly one promotion receipt per payload")
    declarations_by_id = {
        declaration.get("payload_id"): declaration
        for declaration in declarations
        if isinstance(declaration, dict)
    }
    _require(len(declarations_by_id) == len(payloads),
             "ready redistribution promotion receipts must have unique payload ids")
    approval_kinds = {
        declaration.get("approval_kind")
        for declaration in declarations
        if isinstance(declaration, dict) and isinstance(declaration.get("approval_kind"), str)
    }
    _require(len(approval_kinds) == len(declarations),
             "ready redistribution promotion receipts must name unique approval kinds")
    _require(set(claims) == approval_kinds,
             "payload promotion claims registry must exactly equal referenced approval kinds")
    for record in payloads:
        record_id = str(record.get("id") or "payload")
        provenance = record.get("provenance")
        _require(isinstance(provenance, dict), f"{record_id} provenance must be an object")
        _require(record.get("redistribution_readiness") == "ready",
                 f"{record_id} is not redistribution-ready")
        _require(provenance.get("status") == "verified",
                 f"{record_id} provenance is not verified")
        _require(provenance.get("upstream_checksum_verified") is True,
                 f"{record_id} upstream checksum is not verified")
        for field in ("version", "source", "source_evidence", "build_recipe", "license", "license_evidence"):
            value = provenance.get(field)
            _require(isinstance(value, str) and value and value != "unknown",
                     f"{record_id} provenance {field} is not verified")
        _require(provenance.get("license_notice_in_archive") is True,
                 f"{record_id} does not carry its verified license notice")
        declaration = declarations_by_id.get(record_id)
        _require(declaration is not None, f"{record_id} promotion_receipt_missing")
        _validate_promotion_receipt(repo_root, manifest, record, declaration, claims)


def _machine_supports(machine: Any, required: str) -> bool:
    return isinstance(machine, str) and required in machine.split("+")


def _workflow_payload_ids(repo_root: Path, target: str) -> set[str]:
    workflow_paths = [path for path, declared in RELEASE_WORKFLOW_TARGETS.items() if declared == target]
    _require(len(workflow_paths) == 1, f"release target {target} does not have exactly one canonical workflow")
    workflow_path = workflow_paths[0]
    try:
        workflow_text = (repo_root / workflow_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PayloadIntegrityError(f"cannot read canonical release workflow {workflow_path}: {exc}") from exc
    extraction_pattern = re.compile(
        r"(?m)^[ ]*(?:PYTHONPATH=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)?python\s+-m\s+"
        r"tools\.repo_hygiene\.extract_vendored_native_payload\b[^\r\n]*"
        r"--payload-id\s+([A-Za-z0-9._-]+)\b[^\r\n]*$"
    )
    extraction_commands = [match.group(0).strip() for match in extraction_pattern.finditer(workflow_text)]
    payload_ids = [match.group(1) for match in extraction_pattern.finditer(workflow_text)]
    extractor_references = workflow_text.count("tools.repo_hygiene.extract_vendored_native_payload")
    payload_flag_references = len(re.findall(r"--payload-id\b", workflow_text))
    _require(
        extractor_references == len(extraction_commands)
        and payload_flag_references == len(extraction_commands),
        f"canonical release workflow {workflow_path} contains a payload extraction that is not "
        "a complete single-line command",
    )
    _require(payload_ids, f"canonical release workflow {workflow_path} has no payload extraction commands")
    _require(
        len(payload_ids) == len(set(payload_ids)),
        f"canonical release workflow {workflow_path} repeats a payload id",
    )
    for payload_id, command in zip(payload_ids, extraction_commands, strict=True):
        _executable_workflow_marker(
            workflow_text,
            command,
            f"canonical release workflow {workflow_path} payload {payload_id}",
        )
    return set(payload_ids)


WOW64_COMPATIBILITY_MODES = {"wow64-child-process"}
WOW64_COMPATIBILITY_STATUS = "active-release-workflow-wow64-compat"
WOW64_COMPATIBILITY_BINARY = {"format": "pe", "machine": "x86"}


def _validate_compatibility_group(record: dict[str, Any], consumer: dict[str, Any]) -> list[dict[str, Any]]:
    """Admit one hash-bound WOW64 child-process launcher/dependency group.

    Never relaxes the target's own architecture policy: the admitted rows are marked
    with an explicit ``compatibility_mode`` so a caller can tell them apart from a
    native x86_64 member, and the group must exactly equal ``selected_members`` --
    no extra, missing, or renamed member is admitted.
    """
    payload_id = str(record.get("id") or "payload")
    compatibility = consumer.get("compatibility")
    _require(payload_id == "raw2mlv-windows-x86-compat",
             "WOW64 compatibility is limited to the reviewed raw2mlv-windows-x86-compat payload")
    _require(consumer.get("target") == "windows-x86_64",
             "WOW64 compatibility requires target windows-x86_64")
    _require(isinstance(compatibility, dict), f"{payload_id} compat consumer requires a compatibility object")
    _require(set(compatibility) == {"mode", "payload_id", "launcher", "dependencies"},
             f"{payload_id} compatibility fields are incomplete or unexpected")
    _require(compatibility.get("mode") in WOW64_COMPATIBILITY_MODES,
             f"{payload_id} compatibility mode is not a recognized WOW64 child-process mode")
    _require(compatibility.get("payload_id") == payload_id,
             f"{payload_id} compatibility payload_id mismatch")
    launcher = compatibility.get("launcher")
    _require(isinstance(launcher, str) and launcher, f"{payload_id} compatibility launcher must be non-empty text")
    _require(launcher == "raw2mlv.exe", f"{payload_id} compatibility launcher must be raw2mlv.exe")
    dependencies = compatibility.get("dependencies")
    _require(
        isinstance(dependencies, list) and dependencies
        and all(isinstance(item, str) and item for item in dependencies),
        f"{payload_id} compatibility dependencies must be a non-empty list of text",
    )
    _require(len(dependencies) == len(set(dependencies)),
             f"{payload_id} compatibility dependencies repeat a name")
    _require(launcher not in dependencies,
             f"{payload_id} compatibility launcher cannot also be declared a dependency")
    _require(dependencies == ["libraw.dll"], f"{payload_id} compatibility dependencies must be exactly libraw.dll")

    selected_members = record.get("selected_members")
    _require(isinstance(selected_members, list) and selected_members,
             f"{payload_id} selected_members must be a non-empty list")
    by_output: dict[str, dict[str, Any]] = {}
    for selected in selected_members:
        _require(isinstance(selected, dict), f"{payload_id} selected member must be an object")
        output_name = selected.get("output_name")
        _require(isinstance(output_name, str) and output_name,
                 f"{payload_id} selected member output_name is invalid")
        _require(output_name not in by_output,
                 f"{payload_id} selected members repeat output_name {output_name!r}")
        by_output[output_name] = selected

    required_names = {launcher, *dependencies}
    _require(
        set(by_output) == required_names,
        f"{payload_id} compatibility group must exactly match selected_members: "
        f"declared {sorted(required_names)}, selected {sorted(by_output)}",
    )

    admitted: list[dict[str, Any]] = []

    def _admit(output_name: str, expected_kind: str) -> None:
        selected = by_output[output_name]
        _require(selected.get("kind") == expected_kind,
                 f"{payload_id} compatibility member {output_name!r} must be kind {expected_kind}")
        binary = selected.get("binary")
        _require(
            binary == WOW64_COMPATIBILITY_BINARY,
            f"{payload_id} compatibility member {output_name!r} must be an observed x86 PE {expected_kind}",
        )
        member_bytes = _positive_int(selected.get("bytes"), f"{payload_id} compatibility member {output_name!r} bytes")
        member_sha256 = _hex_digest(selected.get("sha256"), f"{payload_id} compatibility member {output_name!r}")
        admitted.append({
            "kind": expected_kind,
            "machine": "x86",
            "output_name": output_name,
            "payload_id": payload_id,
            "compatibility_mode": compatibility["mode"],
            "bytes": member_bytes,
            "sha256": member_sha256,
        })

    _admit(launcher, "executable")
    for dependency in dependencies:
        _admit(dependency, "shared-library")
    return admitted


def _validate_release_target_compatibility(
    repo_root: Path,
    manifest: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    """Require every payload selected by one release target to be native-compatible."""
    policy = RELEASE_TARGET_BINARY_POLICY.get(target)
    _require(policy is not None, f"unsupported release payload target: {target!r}")
    payloads = manifest.get("payloads")
    _require(isinstance(payloads, list), "payload manifest payloads must be a list")
    workflow_payload_ids = _workflow_payload_ids(repo_root, target)
    declared_payload_ids = {
        str(record.get("id"))
        for record in payloads
        if isinstance(record, dict)
        and any(
            isinstance(consumer, dict) and consumer.get("target") == target
            for consumer in record.get("consumers", [])
        )
    }
    _require(
        declared_payload_ids == workflow_payload_ids,
        f"release target {target} extraction/consumer payload mismatch: workflow extracts "
        f"{sorted(workflow_payload_ids)}, manifest declares {sorted(declared_payload_ids)}",
    )
    admitted: list[dict[str, Any]] = []
    for record in payloads:
        _require(isinstance(record, dict), "every payload record must be an object")
        consumers = record.get("consumers")
        _require(isinstance(consumers, list), f"{record.get('id', 'payload')} consumers must be a list")
        matches = [
            consumer
            for consumer in consumers
            if isinstance(consumer, dict) and consumer.get("target") == target
        ]
        _require(len(matches) <= 1, f"{record.get('id', 'payload')} declares target {target} more than once")
        if not matches:
            continue
        consumer = matches[0]
        status = consumer.get("status")
        if status == WOW64_COMPATIBILITY_STATUS and target == "windows-x86_64":
            _require(
                "selected_members" in record,
                f"{record.get('id', 'payload')} compatibility group needs selected_members",
            )
            admitted.extend(_validate_compatibility_group(record, consumer))
            continue
        _require(
            status == "active-release-workflow",
            f"release target {target} rejects payload {record.get('id', 'payload')}: "
            f"consumer status is {status!r}, not 'active-release-workflow'",
        )
        _require(
            "compatibility" not in consumer,
            f"release target {target} rejects payload {record.get('id', 'payload')}: "
            "a plain active-release-workflow consumer must not declare a compatibility exception",
        )
        selected_members = record.get("selected_members")
        _require(
            isinstance(selected_members, list) and selected_members,
            f"{record.get('id', 'payload')} selected_members must be a non-empty list",
        )
        for index, selected in enumerate(selected_members):
            label = f"{record.get('id', 'payload')}.selected_members[{index}]"
            _require(isinstance(selected, dict), f"{label} must be an object")
            binary = selected.get("binary")
            _require(isinstance(binary, dict), f"{label}.binary must be an object")
            _require(
                binary.get("format") == policy["format"],
                f"release target {target} rejects {label}: expected {policy['format']}, "
                f"observed {binary.get('format')!r}",
            )
            _require(
                _machine_supports(binary.get("machine"), policy["machine"]),
                f"release target {target} rejects {label}: required architecture "
                f"{policy['machine']}, observed {binary.get('machine')!r}",
            )
            admitted.append(
                {
                    "kind": selected.get("kind"),
                    "machine": binary["machine"],
                    "output_name": selected.get("output_name"),
                    "payload_id": record.get("id"),
                }
            )
    _require(admitted, f"release target {target} has no admitted native payload members")
    return {"status": "pass", "target": target, "members": admitted}


def _validate_provenance(record: dict[str, Any]) -> None:
    label = str(record.get("id") or "payload")
    provenance = record.get("provenance")
    _require(isinstance(provenance, dict), f"{label} provenance must be an object")
    _require(provenance.get("status") in {"unknown", "partial-local-evidence", "verified"},
             f"{label} provenance status is invalid")

    authoritative = provenance.get("authoritative_source_archive")
    if authoritative is not None:
        _require(isinstance(authoritative, dict),
                 f"{label} authoritative_source_archive must be an object")
        _require(set(authoritative) == {"url", "bytes", "sha256", "tracked_archive_match", "verified_at"},
                 f"{label} authoritative_source_archive fields are incomplete or unexpected")
        _require(authoritative.get("url") == provenance.get("source"),
                 f"{label} authoritative source URL must equal provenance.source")
        _require(authoritative.get("bytes") == record.get("bytes"),
                 f"{label} authoritative source size must equal the tracked archive")
        _require(authoritative.get("sha256") == record.get("sha256"),
                 f"{label} authoritative source sha256 must equal the tracked archive")
        _require(authoritative.get("tracked_archive_match") == "byte-for-byte",
                 f"{label} authoritative source match must be byte-for-byte")
        _require(re.fullmatch(r"20\d\d-[01]\d-[0-3]\d", str(authoritative.get("verified_at"))) is not None,
                 f"{label} authoritative source verification date is invalid")
        _require(provenance.get("status") == "partial-local-evidence",
                 f"{label} archive equality alone must not promote provenance to verified")
        _require(provenance.get("upstream_checksum_verified") is False,
                 f"{label} archive equality must not claim an independently published upstream checksum")

    candidate = provenance.get("candidate_source_mapping")
    if candidate is not None:
        _require(isinstance(candidate, dict), f"{label} candidate_source_mapping must be an object")
        _require(candidate.get("authoritativeness") == "non-authoritative-unverified",
                 f"{label} candidate source mapping must remain explicitly non-authoritative and unverified")
        _require(provenance.get("status") != "verified",
                 f"{label} candidate source mapping cannot promote provenance to verified")
        _require(str(record.get("redistribution_readiness", "")).startswith("blocked-"),
                 f"{label} candidate source mapping cannot reduce its redistribution blocker")
        for key in ("repository", "evidence", "limitation"):
            _require(isinstance(candidate.get(key), str) and candidate[key],
                     f"{label} candidate source mapping {key} is missing")
        revisions = [value for key, value in candidate.items() if key.endswith("revision")]
        _require(revisions and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                                   for value in revisions),
                 f"{label} candidate source revisions must be full 40-character commit ids")


def _validate_archive(
    repo_root: Path,
    record: dict[str, Any],
    policy: dict[str, Any],
    *,
    selected: bool,
    require_tracked: bool = False,
) -> None:
    label = str(record.get("id") or record.get("path") or "payload")
    relative = _manifest_path(record.get("path"), label)
    archive_path = repo_root / Path(relative)
    _require(archive_path.is_file(), f"{label} archive is missing: {relative}")
    expected_bytes = _positive_int(record.get("bytes"), f"{label}.bytes")
    expected_hash = _hex_digest(record.get("sha256"), label)
    _require(expected_bytes <= policy["max_archive_bytes"],
             f"{label} declared archive bytes exceed max_archive_bytes")
    archive_snapshot = _archive_bytes_snapshot(
        repo_root,
        relative,
        expected_bytes,
        expected_hash,
        label,
        require_tracked=require_tracked,
    )
    actual_bytes = len(archive_snapshot)

    archive_format = record.get("archive_format")
    if archive_format is None:
        archive_format = "tar.xz" if relative.lower().endswith(".tar.xz") else "zip"
    _require(archive_format in {"zip", "tar.xz"}, f"{label} archive_format is invalid")
    _require(relative.lower().endswith(".zip") if archive_format == "zip" else relative.lower().endswith(".tar.xz"),
             f"{label} archive_format does not match its path")

    with ArchiveView(archive_snapshot, archive_format, label) as archive:
        observed = _summary(archive.members)
        _validate_summary(record.get("member_summary"), observed, label)
        _require(observed["entries"] <= policy["max_members"], f"{label} exceeds max_members")
        _require(observed["total_uncompressed_bytes"] <= policy["max_total_uncompressed_bytes"],
                 f"{label} exceeds max_total_uncompressed_bytes")
        _require(observed["total_uncompressed_bytes"] / max(1, actual_bytes) <= policy["max_archive_expansion_ratio"],
                 f"{label} exceeds max_archive_expansion_ratio")
        for member in archive.members:
            if member.kind != "file":
                continue
            _require(member.size <= policy["max_single_member_bytes"],
                     f"{label} member {member.name!r} exceeds max_single_member_bytes")
            if member.compressed_size is not None:
                ratio = member.size / max(1, member.compressed_size)
                _require(ratio <= policy["max_member_expansion_ratio"],
                         f"{label} member {member.name!r} exceeds max_member_expansion_ratio")

        if not selected:
            _require("selected_members" not in record, f"{label} inactive artifact must not select runtime payloads")
            return

        selected_members = record.get("selected_members")
        _require(isinstance(selected_members, list) and selected_members,
                 f"{label} selected_members must be a non-empty list")
        selected_names: set[str] = set()
        output_names: set[str] = set()
        for index, selected_record in enumerate(selected_members):
            item_label = f"{label}.selected_members[{index}]"
            _require(isinstance(selected_record, dict), f"{item_label} must be an object")
            member_name = _manifest_path(selected_record.get("path"), item_label)
            _require(member_name not in selected_names, f"{label} selects member {member_name!r} more than once")
            selected_names.add(member_name)
            output_name = _manifest_path(selected_record.get("output_name"), item_label)
            _require("/" not in output_name, f"{item_label}.output_name must be a basename")
            _require(output_name.casefold() not in output_names, f"{label} output_name collision: {output_name!r}")
            output_names.add(output_name.casefold())
            _require(selected_record.get("kind") in {"executable", "shared-library"},
                     f"{item_label}.kind is invalid")
            _positive_int(selected_record.get("bytes"), f"{item_label}.bytes")
            _hex_digest(selected_record.get("sha256"), item_label)
            member = archive.by_name.get(member_name)
            _require(member is not None, f"{label} selected member is missing: {member_name!r}")
            _require(member.kind == "file", f"{label} selected member is not a regular file: {member_name!r}")
            _require(member.size == selected_record["bytes"],
                     f"{item_label} size mismatch: expected {selected_record['bytes']}, got {member.size}")
            if selected_record.get("executable") is True:
                _require(member.mode & 0o111 != 0, f"{item_label} lost its executable mode")
            with archive.open_member(member) as handle:
                member_bytes, member_hash, prefix = _sha256_stream(handle)
            _require(member_bytes == selected_record["bytes"], f"{item_label} streamed size mismatch")
            _require(member_hash == selected_record["sha256"],
                     f"{item_label} sha256 mismatch: expected {selected_record['sha256']}, got {member_hash}")
            expected_binary = selected_record.get("binary")
            _require(isinstance(expected_binary, dict) and set(expected_binary) == {"format", "machine"},
                     f"{item_label}.binary must contain exactly format and machine")
            actual_binary = _binary_identity(prefix)
            actual_format_machine = {
                "format": actual_binary["format"],
                "machine": actual_binary["machine"],
            }
            _require(actual_format_machine == expected_binary,
                     f"{item_label} binary identity mismatch: expected {expected_binary}, got {actual_format_machine}")
            _require(actual_binary["kind"] == selected_record["kind"],
                     f"{item_label} binary kind mismatch: manifest declares {selected_record['kind']}, "
                     f"header identifies {actual_binary['kind']}")


def _git_tracked_artifacts(repo_root: Path, suffixes: list[str]) -> set[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PayloadIntegrityError(f"cannot enumerate Git-tracked payload candidates: {exc}") from exc
    paths = [path.replace("\\", "/") for path in completed.stdout.decode("utf-8").split("\0") if path]
    artifacts: set[str] = set()
    archive_magics = (
        b"PK\x03\x04",
        b"PK\x05\x06",
        b"PK\x07\x08",
        b"\xfd7zXZ\x00",
        b"7z\xbc\xaf\x27\x1c",
        b"Rar!\x1a\x07",
        b"!<arch>\n",
    )
    native_magics = {
        b"\x7fELF",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
    for relative in paths:
        if any(relative.lower().endswith(suffix) for suffix in suffixes):
            artifacts.add(relative)
            continue
        path = repo_root / Path(relative)
        try:
            with path.open("rb") as handle:
                prefix = handle.read(512)
        except OSError as exc:
            raise PayloadIntegrityError(f"cannot inspect Git-tracked file {relative}: {exc}") from exc
        if prefix.startswith(archive_magics) or prefix[:4] in native_magics:
            artifacts.add(relative)
            continue
        if prefix.startswith(b"MZ"):
            try:
                _binary_identity(prefix)
            except PayloadIntegrityError:
                continue
            artifacts.add(relative)
    return artifacts


def _parse_manifest_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadIntegrityError(f"cannot parse payload manifest {label}: {exc}") from exc
    _require(isinstance(manifest, dict), "payload manifest root must be an object")
    return manifest


def validate_manifest(repo_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate an already-parsed immutable manifest snapshot."""
    repo_root = repo_root.resolve()
    _require(manifest.get("schema_version") == 1, "payload manifest schema_version must be 1")
    policy = _validate_policy(manifest.get("integrity_policy"))

    payloads = manifest.get("payloads")
    inactive = manifest.get("inactive_artifacts")
    _require(isinstance(payloads, list) and payloads, "payloads must be a non-empty list")
    _require(isinstance(inactive, list), "inactive_artifacts must be a list")
    records = payloads + inactive
    _require(all(isinstance(record, dict) for record in records), "every payload record must be an object")
    declared_paths = [_manifest_path(record.get("path"), "payload record") for record in records]
    _require(len(declared_paths) == len(set(declared_paths)), "payload manifest declares a path more than once")
    payload_ids = [record.get("id") for record in payloads]
    _require(all(isinstance(value, str) and value for value in payload_ids), "every active payload needs an id")
    _require(len(payload_ids) == len(set(payload_ids)), "payload ids must be unique")

    tracked = _git_tracked_artifacts(repo_root, policy["tracked_artifact_suffixes"])
    declared = set(declared_paths)
    _require(tracked == declared,
             "tracked native/archive inventory mismatch: "
             f"unmanifested={sorted(tracked - declared)}, missing_from_git={sorted(declared - tracked)}")

    for record in payloads:
        _validate_archive(repo_root, record, policy, selected=True, require_tracked=True)
        consumers = record.get("consumers")
        _require(isinstance(consumers, list) and consumers, f"{record['id']} consumers must be a non-empty list")
        for index, consumer in enumerate(consumers):
            _validate_consumer_claim(
                repo_root,
                record["path"],
                consumer,
                f"{record['id']} consumer[{index}]",
            )
        _validate_provenance(record)
    for record in inactive:
        _validate_archive(repo_root, record, policy, selected=False, require_tracked=True)

    readiness = manifest.get("redistribution_readiness")
    _validate_readiness(repo_root, manifest)
    assert isinstance(readiness, dict)
    blockers = readiness["blockers"]
    return {
        "integrity": "pass",
        "payload_count": len(payloads),
        "inactive_artifact_count": len(inactive),
        "redistribution_readiness": readiness["status"],
        "redistribution_enforcement": readiness["enforcement"],
        "redistribution_blockers": blockers,
    }


def validate(
    repo_root: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    required_target: str | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    manifest_file = manifest_path if manifest_path.is_absolute() else repo_root / manifest_path
    try:
        raw = manifest_file.read_bytes()
    except OSError as exc:
        raise PayloadIntegrityError(f"cannot read payload manifest {manifest_file}: {exc}") from exc
    manifest = _parse_manifest_bytes(raw, str(manifest_file))
    result = validate_manifest(repo_root, manifest)
    if required_target is not None:
        result["target_compatibility"] = _validate_release_target_compatibility(
            repo_root,
            manifest,
            required_target,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--require-redistribution-ready",
        action="store_true",
        help="also fail when the separately tracked redistribution-readiness status is blocked",
    )
    parser.add_argument(
        "--require-target-compatible",
        choices=sorted(RELEASE_TARGET_BINARY_POLICY),
        help="fail unless every native payload selected by this release target has the required architecture",
    )
    args = parser.parse_args(argv)
    try:
        result = validate(
            args.repo_root,
            args.manifest,
            required_target=args.require_target_compatible,
        )
    except PayloadIntegrityError as exc:
        print(f"vendored native payload integrity: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.require_redistribution_ready and result["redistribution_readiness"] != "ready":
        print("vendored native payload redistribution readiness: BLOCKED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
