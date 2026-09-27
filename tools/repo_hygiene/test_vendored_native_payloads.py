from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from .extract_vendored_native_payload import (
    extract_payload_record,
    load_validated_manifest_snapshot,
    verify_installed_payload,
)
from .vendored_native_payloads import (
    ArchiveView,
    DEFAULT_MANIFEST,
    PROMOTION_CLAIMS_PATH,
    PROMOTION_VERIFIER_SURFACES,
    PayloadIntegrityError,
    _binary_identity,
    _canonical_sha256,
    _git_tracked_artifacts,
    _manifest_promotion_binding,
    _payload_promotion_identity,
    _promotion_approval_body,
    _validate_promotion_verifier_unchanged,
    _validate_archive,
    _validate_consumer_claim,
    _validate_policy,
    _validate_provenance,
    _validate_readiness,
    _validate_compatibility_group,
    _validate_release_target_compatibility,
    main,
    validate,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


def _fake_pe(machine: int = 0x8664, fill: bytes = b"payload", *, dll: bool = False) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    struct.pack_into("<H", payload, 0x80 + 22, 0x2000 if dll else 0)
    payload[0x100 : 0x100 + len(fill)] = fill
    return bytes(payload)


def _fake_elf(*, elf_type: int, interp: bool) -> bytes:
    payload = bytearray(256)
    payload[:4] = b"\x7fELF"
    payload[4:6] = b"\x02\x01"  # ELF64, little-endian
    struct.pack_into("<H", payload, 16, elf_type)
    struct.pack_into("<H", payload, 18, 62)  # x86_64
    struct.pack_into("<I", payload, 20, 1)
    struct.pack_into("<Q", payload, 32, 64 if interp else 0)
    struct.pack_into("<H", payload, 52, 64)
    struct.pack_into("<H", payload, 54, 56)
    struct.pack_into("<H", payload, 56, 1 if interp else 0)
    if interp:
        struct.pack_into("<I", payload, 64, 3)  # PT_INTERP
    return bytes(payload)


def _fake_macho(
    *,
    cpu_type: int = 0x01000007,
    file_type: int = 2,
    byte_order: str = "<",
) -> bytes:
    payload = bytearray(64)
    payload[:4] = b"\xcf\xfa\xed\xfe" if byte_order == "<" else b"\xfe\xed\xfa\xcf"
    struct.pack_into(f"{byte_order}I", payload, 4, cpu_type)
    struct.pack_into(f"{byte_order}I", payload, 8, 3)
    struct.pack_into(f"{byte_order}I", payload, 12, file_type)
    return bytes(payload)


def _fake_fat_macho(
    *,
    file_types: tuple[int, int] = (2, 2),
    is_64: bool = False,
    byte_order: str = ">",
) -> bytes:
    payload = bytearray(768)
    magics = {
        (False, ">"): b"\xca\xfe\xba\xbe",
        (False, "<"): b"\xbe\xba\xfe\xca",
        (True, ">"): b"\xca\xfe\xba\xbf",
        (True, "<"): b"\xbf\xba\xfe\xca",
    }
    payload[:4] = magics[(is_64, byte_order)]
    struct.pack_into(f"{byte_order}I", payload, 4, 2)
    entry_size = 32 if is_64 else 20
    for index, (cpu_type, file_type, offset) in enumerate(
        ((0x01000007, file_types[0], 256), (0x0100000C, file_types[1], 512))
    ):
        entry = 8 + index * entry_size
        struct.pack_into(f"{byte_order}I", payload, entry, cpu_type)
        struct.pack_into(f"{byte_order}I", payload, entry + 4, 0)
        if is_64:
            struct.pack_into(f"{byte_order}Q", payload, entry + 8, offset)
            struct.pack_into(f"{byte_order}Q", payload, entry + 16, 64)
        else:
            struct.pack_into(f"{byte_order}I", payload, entry + 8, offset)
            struct.pack_into(f"{byte_order}I", payload, entry + 12, 64)
        payload[offset : offset + 64] = _fake_macho(cpu_type=cpu_type, file_type=file_type)
    return bytes(payload)


class VendoredNativePayloadTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((ROOT / DEFAULT_MANIFEST).read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = copy.deepcopy(self.manifest["integrity_policy"])
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        claims_path = self.root / PROMOTION_CLAIMS_PATH
        claims_path.parent.mkdir(parents=True, exist_ok=True)
        claims_path.write_text('{"schema_version":1,"claims":[]}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--", PROMOTION_CLAIMS_PATH.as_posix()], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_zip(
        self,
        entries: list[tuple[str, bytes, int | None]],
        *,
        compression: int = zipfile.ZIP_STORED,
    ) -> Path:
        path = self.root / "fixture.zip"
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, content, mode in entries:
                info = zipfile.ZipInfo(name)
                info.compress_type = compression
                if mode is not None:
                    info.create_system = 3
                    info.external_attr = mode << 16
                archive.writestr(info, content)
        return path

    def _record(self, path: Path, member_name: str, content: bytes) -> dict:
        archive_bytes = path.read_bytes()
        return {
            "id": "fixture",
            "path": path.name,
            "archive_format": "zip",
            "bytes": len(archive_bytes),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "member_summary": {
                "entries": 1,
                "file_entries": 1,
                "directory_entries": 0,
                "total_uncompressed_bytes": len(content),
            },
            "selected_members": [
                {
                    "path": member_name,
                    "output_name": "tool.exe",
                    "kind": "executable",
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "binary": {"format": "pe", "machine": "x86_64"},
                }
            ],
        }

    def _two_member_record(self, path: Path, executable: bytes, library: bytes) -> dict:
        archive_bytes = path.read_bytes()
        return {
            "id": "fixture",
            "path": path.name,
            "archive_format": "zip",
            "bytes": len(archive_bytes),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "member_summary": {
                "entries": 2,
                "file_entries": 2,
                "directory_entries": 0,
                "total_uncompressed_bytes": len(executable) + len(library),
            },
            "selected_members": [
                {
                    "path": "tool.exe",
                    "output_name": "tool.exe",
                    "kind": "executable",
                    "bytes": len(executable),
                    "sha256": hashlib.sha256(executable).hexdigest(),
                    "binary": {"format": "pe", "machine": "x86_64"},
                },
                {
                    "path": "library.dll",
                    "output_name": "library.dll",
                    "kind": "shared-library",
                    "bytes": len(library),
                    "sha256": hashlib.sha256(library).hexdigest(),
                    "binary": {"format": "pe", "machine": "x86_64"},
                },
            ],
        }

    def test_committed_manifest_integrity_passes_but_redistribution_remains_blocked(self) -> None:
        result = validate(ROOT)
        self.assertEqual(result["integrity"], "pass")
        # 10 since PROD-UPSTREAM-SYNC-1 (PR #132): upstream 877dea2c added the arm64 FFmpeg archive
        # platform/qt/FFmpeg/ffmpegOSXarm.zip, registered as payload ffmpeg-macos-arm64.
        self.assertEqual(result["payload_count"], 10)
        self.assertEqual(result["inactive_artifact_count"], 1)
        self.assertEqual(result["redistribution_readiness"], "blocked")
        self.assertEqual(result["redistribution_enforcement"], "advisory")
        self.assertGreaterEqual(len(result["redistribution_blockers"]), 4)
        self.assertEqual(self.manifest["redistribution_readiness"]["promotion_receipts"], [])
        self.assertEqual(
            json.loads((ROOT / PROMOTION_CLAIMS_PATH).read_text(encoding="utf-8")),
            {"schema_version": 1, "claims": []},
        )
        schema = json.loads(
            (ROOT / "tools" / "gates" / "payload-provenance-promotion-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            "mlvapp.payload-provenance-promotion-receipt.v1",
        )
        self.assertEqual(schema["properties"]["authority"]["const"]["automatic_launch_gate"], "CLOSED")

    def test_manifest_keeps_unknown_raw2mlv_provenance_and_arm64_ffmpeg_debt_explicit(self) -> None:
        raw_records = [record for record in self.manifest["payloads"] if record["id"].startswith("raw2mlv-")]
        self.assertEqual(len(raw_records), 5)
        self.assertTrue(all(record["provenance"]["status"] == "unknown" for record in raw_records))
        self.assertTrue(all(record["provenance"]["license"] == "unknown" for record in raw_records))
        mac_ffmpeg = next(record for record in self.manifest["payloads"] if record["id"] == "ffmpeg-macos-x86_64")
        arm_consumer = next(row for row in mac_ffmpeg["consumers"] if row["target"] == "macos-arm64")
        self.assertEqual(arm_consumer["status"], "active-release-workflow-architecture-mismatch")
        self.assertEqual(mac_ffmpeg["selected_members"][0]["binary"]["machine"], "x86_64")

    def test_arm64_release_target_rejects_committed_x86_64_ffmpeg_before_packaging(self) -> None:
        with self.assertRaisesRegex(
            PayloadIntegrityError,
            "release target macos-arm64 rejects payload ffmpeg-macos-x86_64",
        ):
            validate(ROOT, required_target="macos-arm64")

        intel = validate(ROOT, required_target="macos-x86_64")
        admitted = intel["target_compatibility"]
        self.assertEqual(admitted["status"], "pass")
        self.assertEqual(admitted["target"], "macos-x86_64")
        self.assertEqual(
            {row["payload_id"] for row in admitted["members"]},
            {"ffmpeg-macos-x86_64", "raw2mlv-macos-x86_64"},
        )

        relabeled = copy.deepcopy(
            next(record for record in self.manifest["payloads"] if record["id"] == "ffmpeg-macos-x86_64")
        )
        arm_consumer = next(
            consumer for consumer in relabeled["consumers"] if consumer["target"] == "macos-arm64"
        )
        arm_consumer["target"] = "macos-arm64-bypass"
        with self.assertRaisesRegex(PayloadIntegrityError, "does not match canonical release workflow"):
            _validate_consumer_claim(
                ROOT,
                relabeled["path"],
                arm_consumer,
                "relabeled Arm64 ffmpeg consumer",
            )

        omitted = copy.deepcopy(self.manifest)
        omitted_ffmpeg = next(
            record for record in omitted["payloads"] if record["id"] == "ffmpeg-macos-x86_64"
        )
        omitted_ffmpeg["consumers"] = [
            consumer for consumer in omitted_ffmpeg["consumers"] if consumer["target"] != "macos-arm64"
        ]
        self.assertEqual(validate_manifest(ROOT, omitted)["integrity"], "pass")
        with self.assertRaisesRegex(PayloadIntegrityError, "extraction/consumer payload mismatch"):
            _validate_release_target_compatibility(ROOT, omitted, "macos-arm64")

    def test_target_admission_accepts_universal_macho_only_with_requested_slice(self) -> None:
        workflow = self.root / ".github" / "workflows" / "macOS-Arm64.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Extract\n"
            "      run: |\n"
            "        python -m tools.repo_hygiene.extract_vendored_native_payload "
            "--payload-id universal-helper\n",
            encoding="utf-8",
        )
        manifest = {
            "payloads": [
                {
                    "id": "universal-helper",
                    "selected_members": [
                        {
                            "output_name": "helper",
                            "kind": "executable",
                            "binary": {"format": "macho", "machine": "arm64+x86_64"},
                        }
                    ],
                    "consumers": [
                        {"target": "macos-arm64", "status": "active-release-workflow"}
                    ],
                }
            ]
        }
        result = _validate_release_target_compatibility(self.root, manifest, "macos-arm64")
        self.assertEqual(result["members"][0]["machine"], "arm64+x86_64")

        workflow.write_text(
            workflow.read_text(encoding="utf-8")
            + "    - name: Hidden extraction\n"
            + "      run: |\n"
            + "        python -m tools.repo_hygiene.extract_vendored_native_payload \\\n"
            + "          --payload-id hidden-helper\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "not a complete single-line command"):
            _validate_release_target_compatibility(self.root, manifest, "macos-arm64")
        workflow.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Extract\n"
            "      run: |\n"
            "        python -m tools.repo_hygiene.extract_vendored_native_payload "
            "--payload-id universal-helper\n",
            encoding="utf-8",
        )

        incompatible = copy.deepcopy(manifest)
        incompatible["payloads"][0]["selected_members"][0]["binary"]["machine"] = "x86_64"
        with self.assertRaisesRegex(PayloadIntegrityError, "required architecture arm64"):
            _validate_release_target_compatibility(self.root, incompatible, "macos-arm64")

    def test_windows_release_target_admits_wow64_raw2mlv_compat_group_explicitly(self) -> None:
        result = validate(ROOT, required_target="windows-x86_64")
        admitted = result["target_compatibility"]
        self.assertEqual(admitted["status"], "pass")
        self.assertEqual(admitted["target"], "windows-x86_64")
        compat_rows = [row for row in admitted["members"] if row["payload_id"] == "raw2mlv-windows-x86-compat"]
        self.assertEqual({row["output_name"] for row in compat_rows}, {"raw2mlv.exe", "libraw.dll"})
        for row in compat_rows:
            self.assertEqual(row["machine"], "x86")
            self.assertEqual(row["compatibility_mode"], "wow64-child-process")
        ffmpeg_rows = [row for row in admitted["members"] if row["payload_id"] == "ffmpeg-windows-x86_64"]
        self.assertEqual(ffmpeg_rows[0]["machine"], "x86_64")
        self.assertNotIn("compatibility_mode", ffmpeg_rows[0])

    def _windows_compat_record(self) -> dict:
        return copy.deepcopy(
            next(record for record in self.manifest["payloads"] if record["id"] == "raw2mlv-windows-x86-compat")
        )

    def _windows_compat_consumer(self, record: dict) -> dict:
        return next(consumer for consumer in record["consumers"] if consumer["target"] == "windows-x86_64")

    def test_windows_compat_group_rejects_mutated_dependency_hash(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        record = next(record for record in manifest["payloads"] if record["id"] == "raw2mlv-windows-x86-compat")
        dependency = next(m for m in record["selected_members"] if m["output_name"] == "libraw.dll")
        dependency["sha256"] = "0" * 64
        with self.assertRaisesRegex(PayloadIntegrityError, "sha256"):
            validate_manifest(ROOT, manifest)

    def test_windows_compatibility_is_limited_to_the_reviewed_payload_and_pair(self) -> None:
        for changed_field in ("payload", "launcher", "dependency", "target"):
            with self.subTest(changed_field=changed_field):
                record = self._windows_compat_record()
                consumer = self._windows_compat_consumer(record)
                if changed_field == "payload":
                    record["id"] = consumer["compatibility"]["payload_id"] = "another-payload"
                elif changed_field == "launcher":
                    consumer["compatibility"]["launcher"] = "other.exe"
                    record["selected_members"][0]["output_name"] = "other.exe"
                elif changed_field == "dependency":
                    consumer["compatibility"]["dependencies"] = ["other.dll"]
                    record["selected_members"][1]["output_name"] = "other.dll"
                else:
                    consumer["target"] = "linux-x86_64"
                with self.assertRaises(PayloadIntegrityError):
                    _validate_compatibility_group(record, consumer)

    def test_windows_compat_group_rejects_missing_dependency(self) -> None:
        record = self._windows_compat_record()
        consumer = self._windows_compat_consumer(record)
        record["selected_members"] = [m for m in record["selected_members"] if m["output_name"] != "libraw.dll"]
        with self.assertRaisesRegex(
            PayloadIntegrityError, "compatibility group must exactly match selected_members"
        ):
            _validate_compatibility_group(record, consumer)

    def test_windows_compat_group_rejects_renamed_launcher(self) -> None:
        record = self._windows_compat_record()
        consumer = self._windows_compat_consumer(record)
        launcher = next(m for m in record["selected_members"] if m["output_name"] == "raw2mlv.exe")
        launcher["output_name"] = "raw2mlv-renamed.exe"
        with self.assertRaisesRegex(
            PayloadIntegrityError, "compatibility group must exactly match selected_members"
        ):
            _validate_compatibility_group(record, consumer)

    def test_windows_compat_group_rejects_extra_third_binary(self) -> None:
        record = self._windows_compat_record()
        consumer = self._windows_compat_consumer(record)
        extra = copy.deepcopy(record["selected_members"][1])
        extra["output_name"] = "extra.dll"
        record["selected_members"].append(extra)
        with self.assertRaisesRegex(
            PayloadIntegrityError, "compatibility group must exactly match selected_members"
        ):
            _validate_compatibility_group(record, consumer)

    def test_windows_compat_group_rejects_wrong_mode(self) -> None:
        record = self._windows_compat_record()
        consumer = self._windows_compat_consumer(record)
        consumer["compatibility"]["mode"] = "in-process-load"
        with self.assertRaisesRegex(PayloadIntegrityError, "not a recognized WOW64 child-process mode"):
            _validate_compatibility_group(record, consumer)

    def test_windows_compat_group_rejects_wrong_kind(self) -> None:
        record = self._windows_compat_record()
        consumer = self._windows_compat_consumer(record)
        launcher = next(m for m in record["selected_members"] if m["output_name"] == "raw2mlv.exe")
        launcher["kind"] = "shared-library"
        with self.assertRaisesRegex(PayloadIntegrityError, "must be kind executable"):
            _validate_compatibility_group(record, consumer)

    def test_windows_x86_main_executable_still_rejected_by_release_target_policy(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        ffmpeg = next(record for record in manifest["payloads"] if record["id"] == "ffmpeg-windows-x86_64")
        ffmpeg["selected_members"][0]["binary"]["machine"] = "x86"
        with self.assertRaisesRegex(PayloadIntegrityError, "release target windows-x86_64 rejects"):
            _validate_release_target_compatibility(ROOT, manifest, "windows-x86_64")

    def test_undeclared_x86_dll_in_windows_bundle_remains_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        raw = next(record for record in manifest["payloads"] if record["id"] == "raw2mlv-windows-x86-compat")
        consumer = next(c for c in raw["consumers"] if c["target"] == "windows-x86_64")
        consumer["status"] = "active-release-workflow"
        del consumer["compatibility"]
        with self.assertRaisesRegex(PayloadIntegrityError, "release target windows-x86_64 rejects"):
            _validate_release_target_compatibility(ROOT, manifest, "windows-x86_64")

    def test_plain_active_release_consumer_cannot_smuggle_a_compatibility_exception(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        raw = next(record for record in manifest["payloads"] if record["id"] == "raw2mlv-windows-x86-compat")
        consumer = next(c for c in raw["consumers"] if c["target"] == "windows-x86_64")
        consumer["status"] = "active-release-workflow"
        with self.assertRaisesRegex(PayloadIntegrityError, "must not declare a compatibility exception"):
            _validate_release_target_compatibility(ROOT, manifest, "windows-x86_64")

    def test_wow64_compat_status_is_not_recognized_off_windows_x86_64(self) -> None:
        workflow = self.root / ".github" / "workflows" / "macOS-Arm64.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Extract\n"
            "      run: |\n"
            "        python -m tools.repo_hygiene.extract_vendored_native_payload "
            "--payload-id helper\n",
            encoding="utf-8",
        )
        manifest = {
            "payloads": [
                {
                    "id": "helper",
                    "selected_members": [
                        {
                            "output_name": "helper.exe",
                            "kind": "executable",
                            "binary": {"format": "pe", "machine": "x86"},
                        },
                        {
                            "output_name": "helper.dll",
                            "kind": "shared-library",
                            "binary": {"format": "pe", "machine": "x86"},
                        },
                    ],
                    "consumers": [
                        {
                            "target": "macos-arm64",
                            "status": "active-release-workflow-wow64-compat",
                            "compatibility": {
                                "mode": "wow64-child-process",
                                "payload_id": "helper",
                                "launcher": "helper.exe",
                                "dependencies": ["helper.dll"],
                            },
                        }
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(PayloadIntegrityError, "not 'active-release-workflow'"):
            _validate_release_target_compatibility(self.root, manifest, "macos-arm64")

    def test_provenance_truth_keeps_archive_equality_separate_from_upstream_checksum(self) -> None:
        linux_ffmpeg = next(
            record for record in self.manifest["payloads"] if record["id"] == "ffmpeg-linux-x86_64"
        )
        provenance = linux_ffmpeg["provenance"]
        source_archive = provenance["authoritative_source_archive"]
        self.assertEqual(source_archive["url"], provenance["source"])
        self.assertEqual(source_archive["bytes"], linux_ffmpeg["bytes"])
        self.assertEqual(source_archive["sha256"], linux_ffmpeg["sha256"])
        self.assertEqual(source_archive["tracked_archive_match"], "byte-for-byte")
        self.assertEqual(provenance["status"], "partial-local-evidence")
        self.assertFalse(provenance["upstream_checksum_verified"])
        _validate_provenance(linux_ffmpeg)

        promoted = copy.deepcopy(linux_ffmpeg)
        promoted["provenance"]["status"] = "verified"
        with self.assertRaisesRegex(PayloadIntegrityError, "must not promote"):
            _validate_provenance(promoted)

    def test_candidate_source_mappings_are_non_authoritative_and_do_not_reduce_blockers(self) -> None:
        candidate_records = [
            record for record in self.manifest["payloads"]
            if record["id"].startswith("raw2mlv-") or record["id"] == "ffmpeg-macos-x86_64"
        ]
        self.assertEqual(len(candidate_records), 6)
        for record in candidate_records:
            with self.subTest(payload=record["id"]):
                mapping = record["provenance"]["candidate_source_mapping"]
                self.assertEqual(mapping["authoritativeness"], "non-authoritative-unverified")
                self.assertNotEqual(record["provenance"]["status"], "verified")
                self.assertTrue(record["redistribution_readiness"].startswith("blocked-"))
                _validate_provenance(record)

                overstated = copy.deepcopy(record)
                overstated["provenance"]["candidate_source_mapping"]["authoritativeness"] = "authoritative"
                with self.assertRaisesRegex(PayloadIntegrityError, "explicitly non-authoritative"):
                    _validate_provenance(overstated)

    def test_strict_redistribution_mode_stays_red_without_failing_integrity_mode(self) -> None:
        blocked = {
            "integrity": "pass",
            "payload_count": 9,
            "inactive_artifact_count": 1,
            "redistribution_readiness": "blocked",
            "redistribution_enforcement": "advisory",
            "redistribution_blockers": ["unverified"],
        }
        with mock.patch("tools.repo_hygiene.vendored_native_payloads.validate", return_value=blocked), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--require-redistribution-ready"]), 2)

    def test_manifest_strings_cannot_promote_redistribution_without_receipts(self) -> None:
        ready_payload = {
            "id": "verified-tool",
            "path": "verified-tool.zip",
            "archive_format": "zip",
            "bytes": 1,
            "sha256": "a" * 64,
            "member_summary": {},
            "selected_members": [],
            "consumers": [{"fixture": True}],
            "redistribution_readiness": "ready",
            "provenance": {
                "status": "verified",
                "version": "1.2.3",
                "source": "https://example.invalid/source",
                "source_evidence": "reviewed signed release metadata",
                "build_recipe": "reproducible-build.v1",
                "upstream_checksum_verified": True,
                "license": "GPL-3.0-or-later",
                "license_evidence": "reviewed upstream license at pinned source revision",
                "license_notice_in_archive": True,
            },
        }
        manifest = {
            "schema_version": 1,
            "integrity_policy": copy.deepcopy(self.policy),
            "inactive_artifacts": [],
            "payloads": [ready_payload],
            "redistribution_readiness": {
                "status": "ready",
                "enforcement": "required",
                "blockers": [],
                "promotion_receipts": [],
            },
        }
        with self.assertRaisesRegex(PayloadIntegrityError, "requires registered promotion claims"):
            _validate_readiness(self.root, manifest)

        claims_path = self.root / PROMOTION_CLAIMS_PATH
        claims_path.write_text(
            json.dumps({"schema_version": 1, "claims": [{"kind": "fixture-claim"}]}),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", PROMOTION_CLAIMS_PATH.as_posix()],
            check=True,
        )
        candidate = copy.deepcopy(manifest)
        candidate["redistribution_readiness"]["promotion_receipts"] = [
            {"payload_id": "verified-tool", "approval_kind": "fixture-claim"}
        ]
        with self.assertRaisesRegex(PayloadIntegrityError, "target-owned isolated promotion verifier"):
            _validate_readiness(self.root, candidate)

        falsifiers = (
            ("redistribution_readiness", "blocked-unverified", "not redistribution-ready"),
            ("status", "unknown", "provenance is not verified"),
            ("upstream_checksum_verified", False, "upstream checksum is not verified"),
            ("version", "unknown", "provenance version is not verified"),
            ("source", "unknown", "provenance source is not verified"),
            ("source_evidence", "unknown", "provenance source_evidence is not verified"),
            ("build_recipe", "unknown", "provenance build_recipe is not verified"),
            ("license", "unknown", "provenance license is not verified"),
            ("license_evidence", "unknown", "provenance license_evidence is not verified"),
            ("license_notice_in_archive", False, "does not carry its verified license notice"),
        )
        for field, value, expected_error in falsifiers:
            with self.subTest(field=field):
                payload = copy.deepcopy(ready_payload)
                if field == "redistribution_readiness":
                    payload[field] = value
                else:
                    payload["provenance"][field] = value
                candidate = copy.deepcopy(manifest)
                candidate["payloads"] = [payload]
                candidate["redistribution_readiness"]["promotion_receipts"] = [
                    {"payload_id": "verified-tool", "approval_kind": "fixture-claim"}
                ]
                with mock.patch(
                    "tools.repo_hygiene.vendored_native_payloads._validate_promotion_verifier_unchanged"
                ), mock.patch(
                    "tools.repo_hygiene.vendored_native_payloads.PROMOTION_PROVIDER_AUTHORITY_STATE",
                    "INSTALLED_TARGET_OWNED",
                ), self.assertRaisesRegex(PayloadIntegrityError, expected_error):
                    _validate_readiness(self.root, candidate)

    def test_full_validator_can_reach_ready_after_all_integrity_evidence_is_verified(self) -> None:
        executable = _fake_pe()
        notice = b"GPL-3.0-or-later fixture notice\n"
        archive_path = self._write_zip([
            ("tool.exe", executable, stat.S_IFREG | 0o755),
            ("LICENSE.txt", notice, stat.S_IFREG | 0o644),
        ])
        archive_path.rename(self.root / "verified-tool.zip")
        archive_path = self.root / "verified-tool.zip"
        archive_bytes = archive_path.read_bytes()
        evidence_dir = self.root / "evidence"
        evidence_dir.mkdir()
        evidence_specs = {}
        for name, content in {
            "upstream.sha256": hashlib.sha256(archive_bytes).hexdigest().encode("ascii") + b"\n",
            "build-recipe.txt": b"reproducible fixture recipe\n",
            "license-review.txt": b"GPL-3.0-or-later reviewed fixture\n",
        }.items():
            path = evidence_dir / name
            path.write_bytes(content)
            evidence_specs[name] = {
                "path": path.relative_to(self.root).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        payload_record = {
            "id": "verified-tool",
            "path": "verified-tool.zip",
            "archive_format": "zip",
            "bytes": len(archive_bytes),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "member_summary": {
                "entries": 2,
                "file_entries": 2,
                "directory_entries": 0,
                "total_uncompressed_bytes": len(executable) + len(notice),
            },
            "selected_members": [{
                "path": "tool.exe",
                "output_name": "tool.exe",
                "kind": "executable",
                "bytes": len(executable),
                "sha256": hashlib.sha256(executable).hexdigest(),
                "executable": True,
                "binary": {"format": "pe", "machine": "x86_64"},
            }],
            "consumers": [{"fixture": True}],
            "redistribution_readiness": "ready",
            "provenance": {
                "status": "verified",
                "version": "1.2.3",
                "source": "https://example.invalid/source",
                "source_evidence": evidence_specs["upstream.sha256"]["path"],
                "build_recipe": evidence_specs["build-recipe.txt"]["path"],
                "upstream_checksum_verified": True,
                "license": "GPL-3.0-or-later",
                "license_evidence": evidence_specs["license-review.txt"]["path"],
                "license_notice_in_archive": True,
            },
        }
        ready_manifest = {
            "schema_version": 1,
            "integrity_policy": copy.deepcopy(self.policy),
            "redistribution_readiness": {
                "status": "ready",
                "enforcement": "required",
                "blockers": [],
                "promotion_receipts": [],
            },
            "inactive_artifacts": [],
            "payloads": [payload_record],
        }
        receipt = {
            "schema": "mlvapp.payload-provenance-promotion-receipt.v1",
            "payload": {
                "id": "verified-tool",
                "identity_sha256": _canonical_sha256(_payload_promotion_identity(payload_record)),
            },
            "manifest": {"binding_sha256": _manifest_promotion_binding(ready_manifest)},
            "upstream": {
                "source": payload_record["provenance"]["source"],
                "version": payload_record["provenance"]["version"],
                "checksum_evidence": {
                    "algorithm": "sha256",
                    "expected_archive_sha256": payload_record["sha256"],
                    "evidence": evidence_specs["upstream.sha256"],
                },
            },
            "build": {
                "basis": "reproducible-recipe",
                "evidence": evidence_specs["build-recipe.txt"],
            },
            "license": {
                "expression": "GPL-3.0-or-later",
                "evidence": evidence_specs["license-review.txt"],
                "notice_members": [{
                    "path": "LICENSE.txt",
                    "bytes": len(notice),
                    "sha256": hashlib.sha256(notice).hexdigest(),
                }],
            },
            "authority": {
                "repository_owner_approval_required": True,
                "grants_release_publication": False,
                "grants_provider_activation": False,
                "automatic_launch_gate": "CLOSED",
            },
        }
        receipt_dir = self.root / "tools" / "gates" / "payload-provenance"
        receipt_dir.mkdir(parents=True)
        receipt_path = receipt_dir / "verified-tool.json"
        receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_path.write_bytes(receipt_bytes)
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        approval_kind = "verified-tool-owner-approval"
        ready_manifest["redistribution_readiness"]["promotion_receipts"] = [{
            "payload_id": "verified-tool",
            "path": receipt_path.relative_to(self.root).as_posix(),
            "sha256": receipt_sha256,
            "approval_kind": approval_kind,
        }]
        manifest_path = self.root / DEFAULT_MANIFEST
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(ready_manifest), encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "--", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.root),
                "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid",
                "commit", "-qm", "fixture promotion subject",
            ],
            check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        provider_response: dict = {}
        claim: dict | None = None

        def validate_fixture(*, live_response: dict | None = provider_response) -> dict:
            if claim is not None:
                claims_path = self.root / PROMOTION_CLAIMS_PATH
                claims_path.write_text(
                    json.dumps({"schema_version": 1, "claims": [claim]}),
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "-C", str(self.root), "add", "--", PROMOTION_CLAIMS_PATH.as_posix()],
                    check=True,
                )
            with mock.patch(
                "tools.repo_hygiene.vendored_native_payloads._git_tracked_artifacts",
                return_value={"verified-tool.zip"},
            ), mock.patch(
                "tools.repo_hygiene.vendored_native_payloads._validate_consumer_claim"
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance.live_github_issue_comment",
                side_effect=OSError("provider unavailable") if live_response is None else None,
                return_value=None if live_response is None else {"response": live_response},
            ), mock.patch(
                "tools.repo_hygiene.vendored_native_payloads._validate_promotion_verifier_unchanged"
            ), mock.patch(
                "tools.repo_hygiene.vendored_native_payloads.PROMOTION_PROVIDER_AUTHORITY_STATE",
                "INSTALLED_TARGET_OWNED",
            ):
                return validate(self.root, manifest_path)

        with self.assertRaisesRegex(
            PayloadIntegrityError, "requires registered promotion claims"
        ):
            validate_fixture()
        claim = {
            "schema_version": 1,
            "kind": approval_kind,
            "verdict": "APPROVE",
            "payload_id": "verified-tool",
            "receipt_path": receipt_path.relative_to(self.root).as_posix(),
            "receipt_sha256": receipt_sha256,
            "payload_identity_sha256": receipt["payload"]["identity_sha256"],
            "manifest_binding_sha256": receipt["manifest"]["binding_sha256"],
            "reviewed_commit": reviewed_commit,
            "source": {
                "kind": "github_issue_comment",
                "repository": "layibabalola/MLV-App",
                "pull_request": 1,
                "comment_id": 2,
                "html_url": "https://github.com/layibabalola/MLV-App/pull/1#issuecomment-2",
                "created_at": "2026-08-20T12:00:00Z",
                "reviewer": "repository-owner",
                "author_association": "OWNER",
                "body_sha256": "0" * 64,
            },
            "scope": {
                "payload_redistribution": True,
                "release_publication": False,
                "provider_activation": False,
                "automatic_launch_gate": "CLOSED",
            },
        }
        comment_body = _promotion_approval_body(claim)
        claim["source"]["body_sha256"] = hashlib.sha256(comment_body.encode("utf-8")).hexdigest()
        provider_response.update({
            "id": 2,
            "html_url": "https://github.com/layibabalola/MLV-App/pull/1#issuecomment-2",
            "issue_url": "https://api.github.com/repos/layibabalola/MLV-App/issues/1",
            "created_at": "2026-08-20T12:00:00Z",
            "user": {"login": "repository-owner"},
            "author_association": "OWNER",
            "body": comment_body,
        })
        claim["source"]["author_association"] = "MEMBER"
        with self.assertRaisesRegex(PayloadIntegrityError, "must come from a repository OWNER"):
            validate_fixture()
        claim["source"]["author_association"] = "OWNER"

        with self.assertRaisesRegex(PayloadIntegrityError, "could not be verified against canonical GitHub"):
            validate_fixture(live_response=None)

        forged_response = copy.deepcopy(provider_response)
        forged_response["author_association"] = "MEMBER"
        with self.assertRaisesRegex(PayloadIntegrityError, "not authored by a repository OWNER"):
            validate_fixture(live_response=forged_response)

        replayed_response = copy.deepcopy(provider_response)
        replayed_response["body"] = "APPROVE an unrelated change"
        claim["source"]["body_sha256"] = hashlib.sha256(
            replayed_response["body"].encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(PayloadIntegrityError, "does not bind the exact promotion claim"):
            validate_fixture(live_response=replayed_response)
        claim["source"]["body_sha256"] = hashlib.sha256(comment_body.encode("utf-8")).hexdigest()

        tampered = copy.deepcopy(receipt)
        tampered["upstream"]["checksum_evidence"]["expected_archive_sha256"] = "0" * 64
        tampered_bytes = (json.dumps(tampered, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_path.write_bytes(tampered_bytes)
        tampered_sha256 = hashlib.sha256(tampered_bytes).hexdigest()
        ready_manifest["redistribution_readiness"]["promotion_receipts"][0]["sha256"] = tampered_sha256
        claim["receipt_sha256"] = tampered_sha256
        manifest_path.write_text(json.dumps(ready_manifest), encoding="utf-8")
        with self.assertRaisesRegex(PayloadIntegrityError, "does not bind the tracked archive"):
            validate_fixture()

        receipt_path.write_bytes(receipt_bytes)
        ready_manifest["redistribution_readiness"]["promotion_receipts"][0]["sha256"] = receipt_sha256
        claim["receipt_sha256"] = receipt_sha256
        manifest_path.write_text(json.dumps(ready_manifest), encoding="utf-8")
        result = validate_fixture()
        self.assertEqual(result["integrity"], "pass")
        self.assertEqual(result["redistribution_readiness"], "ready")
        self.assertEqual(result["redistribution_enforcement"], "required")

    def test_consumer_claims_bind_reference_operation_and_gate_order(self) -> None:
        consumer_path = self.root / ".github" / "workflows" / "Linux.yml"
        consumer_path.parent.mkdir(parents=True)
        gate_marker = "verify payload integrity"
        operation_marker = "extract bundle.zip"
        workflow = lambda first, second: (
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: First operation\n"
            f"      run: {first}\n"
            "    - name: Second operation\n"
            "      run: |\n"
            f"        {second}\n"
        )
        consumer_path.write_text(workflow(gate_marker, operation_marker), encoding="utf-8")
        consumer = {
            "target": "linux-x86_64",
            "path": ".github/workflows/Linux.yml",
            "status": "active-release-workflow",
            "reference_marker": "bundle.zip",
            "operation_marker": operation_marker,
            "integrity_gate": {
                "path": ".github/workflows/Linux.yml",
                "marker": gate_marker,
                "must_precede": operation_marker,
            },
        }
        _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        falsifiers = (
            ("reference_marker", "missing.zip", "exact declared archive basename"),
            ("operation_marker", "missing extract bundle.zip", "operation_marker marker must occur exactly once"),
        )
        for field, value, expected_error in falsifiers:
            with self.subTest(field=field):
                invalid = copy.deepcopy(consumer)
                invalid[field] = value
                with self.assertRaisesRegex(PayloadIntegrityError, expected_error):
                    _validate_consumer_claim(
                        self.root, "payloads/bundle.zip", invalid, "fixture consumer"
                    )

        missing_gate = copy.deepcopy(consumer)
        missing_gate.pop("integrity_gate")
        with self.assertRaisesRegex(PayloadIntegrityError, "missing integrity_gate"):
            _validate_consumer_claim(
                self.root, "payloads/bundle.zip", missing_gate, "fixture consumer"
            )

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n"
            "    - name: Verify\n"
            f"      run: {gate_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "does not precede"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "env:\n"
            f"  INERT_GATE: {gate_marker}\n"
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Extract\n"
            "      run: |\n"
            f"        {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "exact executable command line"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            f"      run: echo {gate_marker}\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "exact executable command line"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            "      if: false\n"
            f"      run: {gate_marker}\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "conditional or fail-open"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        for quoted_key in ('"if"', "'continue-on-error'"):
            consumer_path.write_text(
                "jobs:\n"
                "  release:\n"
                "    steps:\n"
                "    - name: Verify\n"
                f"      {quoted_key}: false\n"
                f"      run: {gate_marker}\n"
                "    - name: Extract\n"
                f"      run: {operation_marker}\n",
                encoding="utf-8",
            )
            with self.subTest(quoted_key=quoted_key), self.assertRaisesRegex(
                PayloadIntegrityError, "conditional or fail-open"
            ):
                _validate_consumer_claim(
                    self.root, "payloads/bundle.zip", consumer, "fixture consumer"
                )

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            "      shell: bash -c '{0} || true'\n"
            f"      run: {gate_marker}\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "conditional or fail-open"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            "      continue-on-error: true\n"
            f"      run: {gate_marker}\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "conditional or fail-open"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "jobs:\n"
            "  verify:\n"
            "    steps:\n"
            "    - name: Verify\n"
            f"      run: {gate_marker}\n"
            "  package:\n"
            "    steps:\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "different workflow jobs"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

        consumer_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            "      run: |\n"
            "        set +e\n"
            f"        {gate_marker}\n"
            "        true\n"
            "    - name: Extract\n"
            f"      run: {operation_marker}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "complete scalar run command"):
            _validate_consumer_claim(self.root, "payloads/bundle.zip", consumer, "fixture consumer")

    def test_consumer_claim_rejects_cross_payload_substitution(self) -> None:
        (self.root / "consumer.yml").write_text(
            "verify payload integrity\nextract ffmpegWin64.zip\n", encoding="utf-8"
        )
        substituted = {
            "target": "windows-x86-compat",
            "path": "consumer.yml",
            "status": "active-release-workflow",
            "reference_marker": "ffmpegWin64.zip",
            "operation_marker": "extract ffmpegWin64.zip",
            "integrity_gate": {
                "path": "consumer.yml",
                "marker": "verify payload integrity",
                "must_precede": "extract ffmpegWin64.zip",
            },
        }
        with self.assertRaisesRegex(PayloadIntegrityError, "exact declared archive basename"):
            _validate_consumer_claim(
                self.root,
                "platform/qt/raw2mlv/raw2mlvWin64.zip",
                substituted,
                "raw2mlv consumer",
            )

    def test_two_hop_consumer_gate_is_bound_to_the_build_boundary(self) -> None:
        (self.root / "project.pro").write_text("unzip payload.zip\n", encoding="utf-8")
        release_path = self.root / ".github" / "workflows" / "Linux.yml"
        release_path.parent.mkdir(parents=True)
        release_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Verify\n"
            "      run: verify payload integrity\n"
            "    - name: Build\n"
            "      run: |\n"
            "        make -j8\n",
            encoding="utf-8",
        )
        consumer = {
            "target": "linux-x86_64",
            "path": "project.pro",
            "status": "active-release-rule",
            "reference_marker": "payload.zip",
            "operation_marker": "unzip payload.zip",
            "integrity_gate": {
                "path": ".github/workflows/Linux.yml",
                "marker": "verify payload integrity",
                "must_precede": "make -j8",
            },
        }
        _validate_consumer_claim(self.root, "payload.zip", consumer, "two-hop consumer")
        release_path.write_text(
            "jobs:\n"
            "  release:\n"
            "    steps:\n"
            "    - name: Build\n"
            "      run: make -j8\n"
            "    - name: Verify\n"
            "      run: verify payload integrity\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "does not precede"):
            _validate_consumer_claim(self.root, "payload.zip", consumer, "two-hop consumer")

    def test_binary_kind_header_boundaries_cover_pe_elf_and_macho(self) -> None:
        self.assertEqual(_binary_identity(_fake_pe())["kind"], "executable")
        self.assertEqual(_binary_identity(_fake_pe(dll=True))["kind"], "shared-library")

        self.assertEqual(_binary_identity(_fake_elf(elf_type=2, interp=False))["kind"], "executable")
        self.assertEqual(_binary_identity(_fake_elf(elf_type=3, interp=True))["kind"], "executable")
        self.assertEqual(_binary_identity(_fake_elf(elf_type=3, interp=False))["kind"], "shared-library")
        with self.assertRaisesRegex(PayloadIntegrityError, "unsupported ELF e_type"):
            _binary_identity(_fake_elf(elf_type=1, interp=False))

        for byte_order in ("<", ">"):
            with self.subTest(macho_byte_order=byte_order):
                self.assertEqual(
                    _binary_identity(_fake_macho(file_type=2, byte_order=byte_order))["kind"],
                    "executable",
                )
                self.assertEqual(
                    _binary_identity(_fake_macho(file_type=6, byte_order=byte_order))["kind"],
                    "shared-library",
                )
        for is_64 in (False, True):
            for byte_order in (">", "<"):
                with self.subTest(fat64=is_64, fat_byte_order=byte_order):
                    fat = {"is_64": is_64, "byte_order": byte_order}
                    identity = _binary_identity(_fake_fat_macho(**fat))
                    self.assertEqual(identity["machine"], "arm64+x86_64")
                    self.assertEqual(identity["kind"], "executable")
                    self.assertEqual(
                        _binary_identity(_fake_fat_macho(file_types=(6, 6), **fat))["kind"],
                        "shared-library",
                    )
                    with self.assertRaisesRegex(PayloadIntegrityError, "slices disagree"):
                        _binary_identity(_fake_fat_macho(file_types=(2, 6), **fat))

    def test_binary_kind_policy_is_exact_and_enforced(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["binary_kind_policy"]["elf"] = "ET_DYN is always a shared library."
        with self.assertRaisesRegex(PayloadIntegrityError, "binary_kind_policy"):
            _validate_policy(policy)

    def test_real_archives_reject_executable_library_kind_swaps(self) -> None:
        mutations = (
            ("raw2mlv-linux-x86_64", "raw2mlv", "shared-library"),
            ("raw2mlv-macos-arm64", "raw2mlv", "shared-library"),
            ("raw2mlv-windows-x86-compat", "raw2mlv.exe", "shared-library"),
            ("raw2mlv-windows-x86-compat", "libraw.dll", "executable"),
        )
        for record_id, output_name, false_kind in mutations:
            with self.subTest(record=record_id, member=output_name, false_kind=false_kind):
                record = copy.deepcopy(
                    next(item for item in self.manifest["payloads"] if item["id"] == record_id)
                )
                selected = next(
                    item for item in record["selected_members"] if item["output_name"] == output_name
                )
                selected["kind"] = false_kind
                with self.assertRaisesRegex(PayloadIntegrityError, "binary kind mismatch"):
                    _validate_archive(ROOT, record, self.policy, selected=True)

    def test_valid_fixture_passes(self) -> None:
        content = _fake_pe()
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        _validate_archive(self.root, self._record(path, "tool.exe", content), self.policy, selected=True)

    def test_verified_extraction_is_byte_identical_atomic_and_verifies_final_install(self) -> None:
        content = _fake_pe(fill=b"byte-identical-output")
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)
        output_dir = self.root / "installed"
        result = extract_payload_record(
            self.root,
            record,
            output_dir,
            archive_reference="fixture.zip",
            verify_installed=True,
        )
        installed = output_dir / "tool.exe"
        self.assertEqual(installed.read_bytes(), content)
        self.assertEqual(result["installed"][0]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertFalse(any(output_dir.glob(".*.tmp")))

        installed.write_bytes(content + b"tampered")
        with self.assertRaisesRegex(PayloadIntegrityError, "size mismatch"):
            verify_installed_payload(record, output_dir)

    def test_two_member_publication_commits_both_new_files(self) -> None:
        executable = _fake_pe(fill=b"new executable")
        library = _fake_pe(fill=b"new library", dll=True)
        path = self._write_zip(
            [
                ("tool.exe", executable, stat.S_IFREG | 0o755),
                ("library.dll", library, stat.S_IFREG | 0o755),
            ]
        )
        record = self._two_member_record(path, executable, library)
        output_dir = self.root / "installed"
        output_dir.mkdir()
        (output_dir / "tool.exe").write_bytes(b"old executable")
        (output_dir / "library.dll").write_bytes(b"old library")

        result = extract_payload_record(
            self.root,
            record,
            output_dir,
            archive_reference="fixture.zip",
            verify_installed=True,
        )

        self.assertEqual((output_dir / "tool.exe").read_bytes(), executable)
        self.assertEqual((output_dir / "library.dll").read_bytes(), library)
        self.assertEqual(
            [row["sha256"] for row in result["installed"]],
            [hashlib.sha256(executable).hexdigest(), hashlib.sha256(library).hexdigest()],
        )
        self.assertFalse(any(output_dir.glob(".vendored-payload-transaction-*")))

    def test_second_member_publication_failure_restores_both_originals(self) -> None:
        executable = _fake_pe(fill=b"new executable")
        library = _fake_pe(fill=b"new library", dll=True)
        path = self._write_zip(
            [
                ("tool.exe", executable, stat.S_IFREG | 0o755),
                ("library.dll", library, stat.S_IFREG | 0o755),
            ]
        )
        record = self._two_member_record(path, executable, library)
        output_dir = self.root / "installed"
        output_dir.mkdir()
        old_executable = b"old executable"
        old_library = b"old library"
        (output_dir / "tool.exe").write_bytes(old_executable)
        (output_dir / "library.dll").write_bytes(old_library)

        real_replace = os.replace
        publication_count = 0

        def fail_second_publication(source: os.PathLike | str | bytes, destination: os.PathLike | str | bytes) -> None:
            nonlocal publication_count
            if Path(source).name.endswith(".tmp"):
                publication_count += 1
                if publication_count == 2:
                    raise OSError("deterministic second publication failure")
            real_replace(source, destination)

        with mock.patch(
            "tools.repo_hygiene.extract_vendored_native_payload.os.replace",
            side_effect=fail_second_publication,
        ), self.assertRaisesRegex(PayloadIntegrityError, "all originals restored"):
            extract_payload_record(
                self.root,
                record,
                output_dir,
                archive_reference="fixture.zip",
                verify_installed=True,
            )

        self.assertEqual(publication_count, 2)
        self.assertEqual((output_dir / "tool.exe").read_bytes(), old_executable)
        self.assertEqual((output_dir / "library.dll").read_bytes(), old_library)
        self.assertFalse(any(output_dir.glob(".vendored-payload-transaction-*")))
        self.assertFalse(any(output_dir.glob(".*.tmp")))

    def test_manifest_snapshot_hash_and_validation_share_the_same_raced_bytes(self) -> None:
        manifest_path = self.root / "manifest.json"
        original = json.dumps({"schema_version": 1, "snapshot": "original"}).encode("utf-8")
        changed = json.dumps({"schema_version": 1, "snapshot": "changed"}).encode("utf-8")
        manifest_path.write_bytes(original)
        real_read_bytes = Path.read_bytes
        read_count = 0

        def read_then_mutate(path: Path) -> bytes:
            nonlocal read_count
            read_count += 1
            snapshot = real_read_bytes(path)
            path.write_bytes(changed)
            return snapshot

        with mock.patch.object(Path, "read_bytes", read_then_mutate), mock.patch(
            "tools.repo_hygiene.extract_vendored_native_payload.validate_manifest"
        ) as validate_snapshot:
            manifest, digest = load_validated_manifest_snapshot(self.root, manifest_path)

        self.assertEqual(read_count, 1)
        self.assertEqual(manifest["snapshot"], "original")
        self.assertEqual(digest, hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest_path.read_bytes(), changed)
        validate_snapshot.assert_called_once_with(self.root, manifest)

    def test_verified_extraction_rejects_traversal_symlink_and_output_collisions(self) -> None:
        content = _fake_pe()
        traversal = self._write_zip([("../tool.exe", content, stat.S_IFREG | 0o755)])
        traversal_record = self._record(traversal, "../tool.exe", content)
        with self.assertRaisesRegex(PayloadIntegrityError, "traverses a parent"):
            extract_payload_record(
                self.root, traversal_record, self.root / "traversal-out", archive_reference="fixture.zip"
            )

        symlink = self._write_zip([("tool.exe", b"target", stat.S_IFLNK | 0o777)])
        symlink_record = self._record(symlink, "tool.exe", b"target")
        with self.assertRaisesRegex(PayloadIntegrityError, "forbidden symlink"):
            extract_payload_record(
                self.root, symlink_record, self.root / "symlink-out", archive_reference="fixture.zip"
            )

        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        collision_record = self._record(path, "tool.exe", content)
        duplicate = copy.deepcopy(collision_record["selected_members"][0])
        duplicate["output_name"] = "TOOL.EXE"
        collision_record["selected_members"].append(duplicate)
        with self.assertRaisesRegex(PayloadIntegrityError, "output_name collision"):
            extract_payload_record(
                self.root, collision_record, self.root / "collision-out", archive_reference="fixture.zip"
            )

    def test_failed_staging_does_not_replace_existing_installed_bytes(self) -> None:
        content = _fake_pe(fill=b"new")
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)
        record["selected_members"][0]["sha256"] = "0" * 64
        output_dir = self.root / "installed"
        output_dir.mkdir()
        destination = output_dir / "tool.exe"
        destination.write_bytes(b"existing")
        with self.assertRaisesRegex(PayloadIntegrityError, "sha256 mismatch"):
            extract_payload_record(
                self.root, record, output_dir, archive_reference="fixture.zip", verify_installed=True
            )
        self.assertEqual(destination.read_bytes(), b"existing")
        self.assertFalse(any(output_dir.glob(".*.tmp")))

    def test_inventory_discovers_extensionless_native_binaries_and_archives(self) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        (self.root / "extensionless-tool").write_bytes(_fake_pe())
        (self.root / "extensionless-fat64").write_bytes(_fake_fat_macho(is_64=True))
        (self.root / "bundle.zip").write_bytes(b"not-yet-a-valid-archive")
        (self.root / "ordinary.txt").write_text("ordinary", encoding="utf-8")
        subprocess.run(
            ["git", "add", "extensionless-tool", "extensionless-fat64", "bundle.zip", "ordinary.txt"],
            cwd=self.root,
            check=True,
        )
        observed = _git_tracked_artifacts(self.root, self.policy["tracked_artifact_suffixes"])
        self.assertEqual(observed, {"extensionless-tool", "extensionless-fat64", "bundle.zip"})

    def test_rejects_archive_size_and_hash_drift(self) -> None:
        content = _fake_pe()
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)
        wrong_size = copy.deepcopy(record)
        wrong_size["bytes"] += 1
        with self.assertRaisesRegex(PayloadIntegrityError, "archive size mismatch"):
            _validate_archive(self.root, wrong_size, self.policy, selected=True)
        wrong_hash = copy.deepcopy(record)
        wrong_hash["sha256"] = "0" * 64
        with self.assertRaisesRegex(PayloadIntegrityError, "archive sha256 mismatch"):
            _validate_archive(self.root, wrong_hash, self.policy, selected=True)

    def test_archive_validation_uses_one_authenticated_immutable_snapshot(self) -> None:
        original_content = _fake_pe(fill=b"original")
        archive_path = self._write_zip([
            ("tool.exe", original_content, stat.S_IFREG | 0o755),
            ("extra.txt", b"original extra", stat.S_IFREG | 0o644),
        ])
        original_bytes = archive_path.read_bytes()
        record = self._record(archive_path, "tool.exe", original_content)
        record["member_summary"] = {
            "entries": 2,
            "file_entries": 2,
            "directory_entries": 0,
            "total_uncompressed_bytes": len(original_content) + len(b"original extra"),
        }
        replacement_content = _fake_pe(fill=b"replaced")
        self._write_zip([
            ("tool.exe", replacement_content, stat.S_IFREG | 0o755),
            ("extra.txt", b"replacement xx", stat.S_IFREG | 0o644),
        ])
        replacement_bytes = archive_path.read_bytes()
        archive_path.write_bytes(original_bytes)
        real_init = ArchiveView.__init__

        def replace_path_before_parse(view: ArchiveView, source: Path | bytes, archive_format: str, label: str) -> None:
            archive_path.write_bytes(replacement_bytes)
            real_init(view, source, archive_format, label)

        with mock.patch.object(ArchiveView, "__init__", new=replace_path_before_parse):
            _validate_archive(self.root, record, self.policy, selected=True)

        self.assertEqual(archive_path.read_bytes(), replacement_bytes)
        self.assertNotEqual(hashlib.sha256(replacement_bytes).hexdigest(), record["sha256"])

    def test_tracked_archive_rejects_reparse_paths_and_nonregular_index_modes(self) -> None:
        content = _fake_pe()
        archive_path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(archive_path, "tool.exe", content)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "--", archive_path.name], check=True)

        with mock.patch(
            "tools.repo_hygiene.vendored_native_payloads.is_reparse_point",
            side_effect=lambda candidate: candidate == archive_path,
        ), self.assertRaisesRegex(PayloadIntegrityError, "reparse-backed"):
            _validate_archive(self.root, record, self.policy, selected=True, require_tracked=True)

        blob = subprocess.run(
            ["git", "-C", str(self.root), "hash-object", "-w", "--", archive_path.name],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.root), "update-index", "--cacheinfo", f"120000,{blob},{archive_path.name}"],
            check=True,
        )
        with self.assertRaisesRegex(PayloadIntegrityError, "stage-0 regular file"):
            _validate_archive(self.root, record, self.policy, selected=True, require_tracked=True)

    def test_tracked_archive_rejects_path_identity_swap_during_open(self) -> None:
        content = _fake_pe()
        archive_path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(archive_path, "tool.exe", content)
        subprocess.run(["git", "-C", str(self.root), "add", "--", archive_path.name], check=True)
        replacement = self.root / "replacement.zip"
        replacement.write_bytes(archive_path.read_bytes())
        real_open = os.open

        def swap_before_open(candidate: os.PathLike[str] | str, flags: int, *args: object, **kwargs: object) -> int:
            if Path(candidate) == archive_path:
                os.replace(replacement, archive_path)
            return real_open(candidate, flags, *args, **kwargs)

        with mock.patch(
            "tools.repo_hygiene.vendored_native_payloads.os.open",
            side_effect=swap_before_open,
        ), self.assertRaisesRegex(PayloadIntegrityError, "identity changed|identity differs"):
            _validate_archive(self.root, record, self.policy, selected=True, require_tracked=True)

    def test_ready_promotion_requires_verifier_bytes_from_canonical_target(self) -> None:
        for relative in PROMOTION_VERIFIER_SURFACES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = "{}\n" if relative == "closeout.config.json" else f"trusted target bytes for {relative}\n"
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "trusted verifier baseline",
            ],
            check=True,
        )
        target = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        with mock.patch(
            "tools.repo_hygiene.candidate_acceptance.live_github_branch_head",
            return_value=target,
        ):
            _validate_promotion_verifier_unchanged(self.root)

            verifier = self.root / "tools" / "repo_hygiene" / "vendored_native_payloads.py"
            baseline_bytes = verifier.read_bytes()
            verifier.write_bytes(baseline_bytes + b"candidate drift\n")
            with self.assertRaisesRegex(PayloadIntegrityError, "differs from the pinned target"):
                _validate_promotion_verifier_unchanged(self.root)
            verifier.write_bytes(baseline_bytes)

            claims_path = self.root / PROMOTION_CLAIMS_PATH
            claims_path.write_text('{"schema_version":1,"claims":[{"kind":"data-only"}]}\n', encoding="utf-8")
            _validate_promotion_verifier_unchanged(self.root)

    def test_rejects_member_summary_size_and_hash_drift(self) -> None:
        content = _fake_pe()
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)
        wrong_summary = copy.deepcopy(record)
        wrong_summary["member_summary"]["entries"] = 2
        with self.assertRaisesRegex(PayloadIntegrityError, "member_summary.entries mismatch"):
            _validate_archive(self.root, wrong_summary, self.policy, selected=True)
        wrong_size = copy.deepcopy(record)
        wrong_size["selected_members"][0]["bytes"] += 1
        with self.assertRaisesRegex(PayloadIntegrityError, "size mismatch"):
            _validate_archive(self.root, wrong_size, self.policy, selected=True)
        wrong_hash = copy.deepcopy(record)
        wrong_hash["selected_members"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(PayloadIntegrityError, "sha256 mismatch"):
            _validate_archive(self.root, wrong_hash, self.policy, selected=True)

    def test_rejects_path_traversal_and_case_collisions(self) -> None:
        content = _fake_pe()
        traversal = self._write_zip([("../tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(traversal, "../tool.exe", content)
        with self.assertRaisesRegex(PayloadIntegrityError, "traverses a parent"):
            _validate_archive(self.root, record, self.policy, selected=True)

        collision = self._write_zip(
            [
                ("Tool.exe", content, stat.S_IFREG | 0o755),
                ("tool.exe", content, stat.S_IFREG | 0o755),
            ]
        )
        record = self._record(collision, "tool.exe", content)
        record["member_summary"] = {
            "entries": 2,
            "file_entries": 2,
            "directory_entries": 0,
            "total_uncompressed_bytes": len(content) * 2,
        }
        with self.assertRaisesRegex(PayloadIntegrityError, "case-colliding"):
            _validate_archive(self.root, record, self.policy, selected=True)

        reserved = self._write_zip([("NUL.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(reserved, "NUL.exe", content)
        with self.assertRaisesRegex(PayloadIntegrityError, "reserved device name"):
            _validate_archive(self.root, record, self.policy, selected=True)

    def test_rejects_symlinks_and_wrong_binary_architecture(self) -> None:
        content = _fake_pe()
        symlink = self._write_zip([("tool.exe", b"target", stat.S_IFLNK | 0o777)])
        record = self._record(symlink, "tool.exe", b"target")
        with self.assertRaisesRegex(PayloadIntegrityError, "forbidden symlink"):
            _validate_archive(self.root, record, self.policy, selected=True)

        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)
        record["selected_members"][0]["binary"]["machine"] = "arm64"
        with self.assertRaisesRegex(PayloadIntegrityError, "binary identity mismatch"):
            _validate_archive(self.root, record, self.policy, selected=True)

    def test_rejects_archive_member_and_expansion_ceilings(self) -> None:
        content = _fake_pe()
        path = self._write_zip([("tool.exe", content, stat.S_IFREG | 0o755)])
        record = self._record(path, "tool.exe", content)

        archive_policy = copy.deepcopy(self.policy)
        archive_policy["max_archive_bytes"] = record["bytes"] - 1
        with self.assertRaisesRegex(PayloadIntegrityError, "declared archive bytes exceed"):
            _validate_archive(self.root, record, archive_policy, selected=True)

        member_policy = copy.deepcopy(self.policy)
        member_policy["max_members"] = 0
        with self.assertRaisesRegex(PayloadIntegrityError, "exceeds max_members"):
            _validate_archive(self.root, record, member_policy, selected=True)

        single_policy = copy.deepcopy(self.policy)
        single_policy["max_single_member_bytes"] = len(content) - 1
        with self.assertRaisesRegex(PayloadIntegrityError, "exceeds max_single_member_bytes"):
            _validate_archive(self.root, record, single_policy, selected=True)

        total_policy = copy.deepcopy(self.policy)
        total_policy["max_total_uncompressed_bytes"] = len(content) - 1
        with self.assertRaisesRegex(PayloadIntegrityError, "exceeds max_total_uncompressed_bytes"):
            _validate_archive(self.root, record, total_policy, selected=True)

        compressed_content = _fake_pe(fill=b"\0" * 200) + b"\0" * 20000
        compressed = self._write_zip(
            [("tool.exe", compressed_content, stat.S_IFREG | 0o755)],
            compression=zipfile.ZIP_DEFLATED,
        )
        compressed_record = self._record(compressed, "tool.exe", compressed_content)
        expansion_policy = copy.deepcopy(self.policy)
        expansion_policy["max_archive_expansion_ratio"] = 1000
        expansion_policy["max_member_expansion_ratio"] = 2
        with self.assertRaisesRegex(PayloadIntegrityError, "exceeds max_member_expansion_ratio"):
            _validate_archive(self.root, compressed_record, expansion_policy, selected=True)

    def test_release_workflows_validate_before_any_payload_extraction(self) -> None:
        command = "python -m tools.repo_hygiene.vendored_native_payloads --repo-root ."
        self.assertEqual((ROOT / ".python-version").read_text(encoding="utf-8").strip(), "3.13.15")
        workflow_expectations = {
            ".github/workflows/Windows.yml": (command, 2),
            ".github/workflows/Linux.yml": (command, 2),
            ".github/workflows/macOS-Intel.yml": (command, 2),
            ".github/workflows/macOS-Arm64.yml": (command, 2),
        }
        helper = "tools.repo_hygiene.extract_vendored_native_payload"
        for relative, (command, expected_helper_calls) in workflow_expectations.items():
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(text.count(command), 1)
                if relative == ".github/workflows/Windows.yml":
                    self.assertIn(command + " --require-target-compatible windows-x86_64", text)
                self.assertIn("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", text)
                self.assertLess(text.index("actions/setup-python@"), text.index(command))
                self.assertEqual(text.count('python-version-file: ".python-version"'), 1)
                self.assertNotRegex(text, r"(?m)^\s+python-version:\s*")
                for lock_path in (".github/requirements/pip.txt", ".github/requirements/repo-hygiene.txt"):
                    install = (
                        "python -m pip install --disable-pip-version-check --no-input "
                        f"--only-binary=:all: --require-hashes -r {lock_path}"
                    )
                    self.assertEqual(text.count(install), 1)
                    self.assertLess(text.index(install), text.index(command))
                self.assertEqual(text.count("python -m pip check"), 1)
                self.assertEqual(text.count(helper), expected_helper_calls)
                self.assertLess(text.index(command), text.index(helper))
                self.assertEqual(text.count("--verify-installed"), expected_helper_calls)

        arm_text = (ROOT / ".github" / "workflows" / "macOS-Arm64.yml").read_text(encoding="utf-8")
        strict_arm_gate = (
            "python -m tools.repo_hygiene.vendored_native_payloads --repo-root . "
            "--require-target-compatible macos-arm64"
        )
        self.assertEqual(arm_text.count(strict_arm_gate), 1)
        self.assertLess(arm_text.index(strict_arm_gate), arm_text.index("- name: Create build directory"))
        self.assertLess(arm_text.index(strict_arm_gate), arm_text.index("extract_vendored_native_payload"))
        self.assertLess(arm_text.index(strict_arm_gate), arm_text.index("- name: Save build artifact"))

        windows = (ROOT / ".github/workflows/Windows.yml").read_text(encoding="utf-8")
        linux = (ROOT / ".github/workflows/Linux.yml").read_text(encoding="utf-8")
        self.assertNotIn("7z x ffmpegWin64.zip", windows)
        self.assertNotIn("7z x raw2mlvWin64.zip", windows)
        self.assertNotIn("tar -C ${{ env.SOURCE_DIR }}/qt/FFmpeg/", linux)
        self.assertNotIn("tar -C ${{ env.SOURCE_DIR }}/qt/raw2mlv/", linux)
        for relative in (".github/workflows/macOS-Intel.yml", ".github/workflows/macOS-Arm64.yml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("MLVAPP_SKIP_LEGACY_PAYLOAD_EXTRACTION=1", text)

        qmake = (ROOT / "platform/qt/MLVApp.pro").read_text(encoding="utf-8")
        self.assertIn("macx:!equals(MLVAPP_SKIP_LEGACY_PAYLOAD_EXTRACTION, 1)", qmake)
        self.assertIn("portable pinned-Python discovery contract", qmake)
        self.assertIn("tar -C $$(HOME)/bin", qmake)

        tests_workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        # This pin exists to guarantee CI RUNS THIS FILE. It used to assert the exact
        # per-file invocation; the workflow now uses a directory target, which runs it by
        # construction and also catches files added later -- a named list silently omits
        # what it forgets to name. Both halves of the mechanism are pinned, so weakening
        # either the search root or the pattern still fails here.
        self.assertIn("unittest discover -s tools/repo_hygiene", tests_workflow)
        self.assertIn('-p "test_*.py"', tests_workflow)
        self.assertIn("python -m tools.repo_hygiene.vendored_native_payloads --repo-root .", tests_workflow)

    def test_macos_release_toolchains_install_before_python_and_fail_closed(self) -> None:
        expected_order = (
            "Install llvm & Qt & OpenSSL",
            "Verify macOS build toolchain",
            "Set up Python for payload verification",
            "Verify vendored native payload integrity",
            "Build",
        )
        expected_verify_lines = (
            'test -x "${{ env.QTDIR }}/qmake"',
            '"${{ env.QTDIR }}/qmake" -v',
            'LLVM_CXX="$(brew --prefix llvm)/bin/clang++"',
            'test -x "$LLVM_CXX"',
            '"$LLVM_CXX" --version',
        )
        permissive_shell = re.compile(
            r"(?im)(?:\|\|\s*(?:true|:)(?:\s|$)|(?:;|&&)\s*true(?:\s|$)|"
            r"^\s*set\s+\+(?:e|o\s+errexit)\b)"
        )

        def named_step_blocks(text: str) -> list[tuple[str, str]]:
            matches = list(re.finditer(r"(?m)^    - name:\s*([^\r\n]+)\s*$", text))
            return [
                (
                    match.group(1).strip(),
                    text[
                        match.start() : matches[index + 1].start()
                        if index + 1 < len(matches)
                        else len(text)
                    ],
                )
                for index, match in enumerate(matches)
            ]

        def normalized_run_lines(block: str) -> tuple[str, ...]:
            lines = block.splitlines()
            run_index = next(
                index for index, line in enumerate(lines) if re.fullmatch(r"\s{6}run:\s*\|\s*", line)
            )
            commands: list[str] = []
            for line in lines[run_index + 1 :]:
                if line.startswith("        "):
                    if line.strip():
                        commands.append(line.strip())
                    continue
                if line.strip():
                    break
            return tuple(commands)

        def assert_fail_closed_policy(text: str) -> None:
            self.assertNotRegex(text, r"(?im)^\s*continue-on-error\s*:")
            steps = named_step_blocks(text)
            names = [name for name, _ in steps]
            for name in expected_order:
                self.assertEqual(names.count(name), 1, f"expected one step named {name!r}")
            positions = [names.index(name) for name in expected_order]
            self.assertEqual(positions, sorted(positions))

            blocks = dict(steps)
            install_block = blocks[expected_order[0]]
            verify_block = blocks[expected_order[1]]
            self.assertIn("brew: llvm qt5 openssl", install_block)
            self.assertNotRegex(install_block, permissive_shell)
            self.assertNotRegex(verify_block, permissive_shell)
            self.assertEqual(normalized_run_lines(verify_block), expected_verify_lines)

        for relative in (
            ".github/workflows/macOS-Intel.yml",
            ".github/workflows/macOS-Arm64.yml",
        ):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                assert_fail_closed_policy(text)

                falsifiers = (
                    (
                        "verify continue-on-error",
                        text.replace(
                            "    - name: Verify macOS build toolchain\n",
                            "    - name: Verify macOS build toolchain\n      continue-on-error: true\n",
                            1,
                        ),
                    ),
                    (
                        "qmake masks failure",
                        text.replace(
                            '"${{ env.QTDIR }}/qmake" -v',
                            '"${{ env.QTDIR }}/qmake" -v || true',
                            1,
                        ),
                    ),
                    (
                        "clang masks failure",
                        text.replace('"$LLVM_CXX" --version', '"$LLVM_CXX" --version || true', 1),
                    ),
                    (
                        "errexit disabled",
                        text.replace(
                            '        test -x "${{ env.QTDIR }}/qmake"',
                            '        set +e\n        test -x "${{ env.QTDIR }}/qmake"',
                            1,
                        ),
                    ),
                )
                for label, falsified in falsifiers:
                    with self.subTest(workflow=relative, falsifier=label):
                        with self.assertRaises(AssertionError):
                            assert_fail_closed_policy(falsified)


if __name__ == "__main__":
    unittest.main()
