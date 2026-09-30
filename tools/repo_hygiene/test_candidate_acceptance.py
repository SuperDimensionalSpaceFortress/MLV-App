from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.repo_hygiene.brokered_closeout import (
    DEFAULT_CLOSEOUT_CONFIG,
    load_closeout_config,
    simulate_clean_integration,
    verify_closeout_tooling_current,
)
from tools.repo_hygiene.candidate_acceptance import (
    _minimal_system_child_environment,
    _trusted_unix_curl_path_chain,
    _WINDOWS_CURL_TRUST_SCRIPT,
    _hash,
    _live_github_provider_payload,
    acceptance_root,
    CONTENT_REVIEWERS,
    candidate_acceptance_enforced,
    candidate_identity,
    content_review_gate_trust_error,
    evaluate,
    final_integration_mismatches,
    latest_summary,
    live_github_branch_head,
    provider_surface_record,
    system_curl_github_query_command,
    surface_record,
    validation_plan,
    validate_for_finalize,
    verify_live_provider,
    write_ledger,
)
from tools.repo_hygiene.core import HygieneError


TARGET = "1" * 40
FEATURE = "2" * 40
REQUIRED = ["content-self", "content-stranger-1", "content-stranger-2", "hosted-tests", "hosted-codeql"]
ROOT = Path(__file__).resolve().parents[2]

# The Windows curl-trust tests drive powershell.exe to build an ACL fixture and then to run the
# production probe against it. A call that does not return means the property under test was NEVER
# CHECKED -- that is UNKNOWN, not a pass and not a plain error -- so it fails as a typed
# WindowsAclProbeUnknown carrying the partial output and the elapsed time. The only escape is the
# named flag below, which a maintainer sets by hand on a host known to stall; CI never sets it
# (pinned by WindowsAclProbeTimeoutTests), and using it logs a line rather than passing quietly.
ALLOW_UNKNOWN_ACL_PROBE_ENV = "MLV_ALLOW_UNKNOWN_WINDOWS_ACL_PROBE"
WINDOWS_ACL_PROBE_TIMEOUT_SECONDS = 180
# Stage markers for the fixture scripts' own stderr: when a probe stalls, the partial stderr in the
# UNKNOWN message says which statement it was inside instead of just "timed out".
_PS_STAGE_TRACE = (
    "$sw=[Diagnostics.Stopwatch]::StartNew();"
    "function T($m){[Console]::Error.WriteLine('stage '+$m+' '+$sw.ElapsedMilliseconds+'ms')};"
)


class WindowsAclProbeUnknown(AssertionError):
    """A powershell.exe ACL probe did not return in time: the check did not run."""


def _run_windows_acl_probe(test: unittest.TestCase, what: str, command: list[str], *, env: dict[str, str],
                           timeout: float = WINDOWS_ACL_PROBE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        def _text(value: object) -> str:
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")

        detail = (
            f"UNKNOWN: powershell probe '{what}' did not return within {timeout}s; the property it "
            f"guards was NOT checked. partial stdout={_text(exc.stdout)[-600:]!r} "
            f"partial stderr={_text(exc.stderr)[-600:]!r}"
        )
        if os.environ.get(ALLOW_UNKNOWN_ACL_PROBE_ENV) == "1":
            print(f"[curl-trust] SKIPPED-UNKNOWN via {ALLOW_UNKNOWN_ACL_PROBE_ENV}=1: {detail}", file=sys.stderr)
            test.skipTest(detail)
        raise WindowsAclProbeUnknown(detail) from exc
    print(f"[curl-trust] probe '{what}' returned in {time.monotonic() - started:.1f}s", file=sys.stderr)
    return completed


class CandidateAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_temp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.provider_temp.name).resolve()
        self.config = {
            "policyHash": "policy-hash",
            "toolingBaseline": {"enabled": True},
            "candidateAcceptance": {
                "enabled": True,
                "requireReadyForFinalize": True,
                "providerRepository": "layibabalola/MLV-App",
                "stateRoot": ".claude-state/closeout/acceptance",
                "requiredSurfaces": REQUIRED,
            },
            "validation": {"commands": []},
        }
        self.enforcement_patcher = mock.patch(
            "tools.repo_hygiene.candidate_acceptance.candidate_acceptance_enforced",
            return_value=True,
        )
        self.enforcement_patcher.start()
        self.addCleanup(self.enforcement_patcher.stop)
        provider_dir = self.repo_root / ".claude-state/closeout/acceptance/provider" / FEATURE
        provider_dir.mkdir(parents=True)
        names = [
            ("Repo Hygiene Python (windows-latest)", 15368),
            ("Repo Hygiene Python (ubuntu-latest)", 15368),
            ("Factory Bridge Regressions", 15368),
            ("Windows GUI Pilot", 15368),
            ("Windows Product Oracles", 15368),
            ("Protected Check Route", 15368),
            ("Analyze (actions)", 15368),
            ("Analyze (c-cpp)", 15368),
            ("Analyze (python)", 15368),
            ("CodeQL", 57789),
        ]
        runs = [
            {
                "name": name,
                "id": index,
                "head_sha": FEATURE,
                "started_at": f"2026-08-20T00:00:{index:02d}Z",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": app_id},
                "output": {"annotations_count": 0},
                "details_url": f"https://example.invalid/check/{index}",
            }
            for index, (name, app_id) in enumerate(names, 1)
        ]
        self.write_provider(
            {
                "schema": "candidate-acceptance-github-checks.v1",
                "capturedAt": "2026-08-20T00:01:00Z",
                "repository": "layibabalola/MLV-App",
                "headSha": FEATURE,
                "response": {"total_count": len(runs), "check_runs": runs},
            }
        )

    def tearDown(self) -> None:
        self.provider_temp.cleanup()

    def write_provider(self, payload: dict) -> Path:
        provider_dir = self.repo_root / ".claude-state/closeout/acceptance/provider" / FEATURE
        provider_dir.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        path = provider_dir / f"check-runs-{digest}.json"
        path.write_bytes(encoded)
        self.provider_path = path
        return path

    def candidate(self, *, clean: bool = True) -> tuple[dict, dict]:
        rehearsal = {
            "clean": clean,
            "reason": "clean_merge_and_validation_passed" if clean else "diff_check_failed",
            "integrationHead": "3" * 40,
            "integrationTree": "4" * 40,
            "diffSha256": "5" * 64,
            "changedPaths": ["src/a.c"],
            "validationStatus": "passed" if clean else "diff_check_failed",
            "validations": [],
        }
        candidate = candidate_identity(
            target_head=TARGET,
            feature_head=FEATURE,
            policy_hash="policy-hash",
            plan_hash=_hash(validation_plan(self.config)),
            rehearsal=rehearsal,
        )
        return candidate, rehearsal

    def records(self, candidate: dict, verdicts: dict[str, str] | None = None) -> list[dict]:
        verdicts = verdicts or {}
        records = []
        for index, surface in enumerate(REQUIRED):
            if surface in {"hosted-tests", "hosted-codeql"}:
                records.append(
                    provider_surface_record(
                        self.repo_root,
                        self.config,
                        candidate,
                        surface=surface,
                        evidence_path=self.provider_path,
                    )
                )
                continue
            records.append(
                surface_record(
                    candidate,
                    surface=surface,
                    verdict=verdicts.get(surface, "APPROVE"),
                    reviewer=CONTENT_REVIEWERS[surface],
                    session_id=f"session-{index}",
                findings=(
                    [{"id": f"finding-{surface}", "invariant": "exact tuple remains valid", "falsifier": "mutate tuple"}]
                    if verdicts.get(surface) == "CHANGES_REQUESTED"
                    else []
                ),
                created_at=f"2026-08-20T00:00:0{index}Z",
                )
            )
        return records

    def run_evaluate(self, *, candidate: dict, rehearsal: dict, records: list[dict], required: list[str] = REQUIRED) -> dict:
        return evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            required=required,
            records=records,
            repo_root=self.repo_root,
            config=self.config,
        )

    def test_all_terminal_approvals_and_clean_rehearsal_are_ready(self) -> None:
        candidate, rehearsal = self.candidate()
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        self.assertEqual("ready", result["state"])
        self.assertEqual([], result["missingSurfaces"])
        self.assertEqual([], result["blockers"])
        self.assertNotIn("fixBatch", result)
        self.assertFalse(result["authorityBoundaries"]["agentApprovalsGrantHumanAuthority"])

    def test_blocker_waits_for_all_surfaces_then_emits_one_fix_batch(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate, {"content-stranger-1": "CHANGES_REQUESTED"})
        collecting = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records[:-1])
        self.assertEqual("collecting", collecting["state"])
        self.assertNotIn("fixBatch", collecting)
        blocked = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("changes_required", blocked["state"])
        self.assertEqual("acceptance-fix-batch.v1", blocked["fixBatch"]["schema"])
        self.assertEqual(["finding-content-stranger-1"], [item["id"] for item in blocked["fixBatch"]["findings"]])

    def test_tuple_drift_never_carries_approval(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        records[0]["featureHead"] = "9" * 40
        clean = dict(records[0])
        clean.pop("recordHash")
        records[0]["recordHash"] = __import__("hashlib").sha256(
            json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("collecting", result["state"])
        self.assertIn("content-self", result["missingSurfaces"])
        self.assertEqual("candidate_tuple_mismatch", result["staleSurfaceRecords"][0]["reason"])

    def test_ephemeral_merge_commit_drift_does_not_invalidate_identical_content_tuple(self) -> None:
        candidate, rehearsal = self.candidate()
        repeated = dict(rehearsal)
        repeated["integrationHead"] = "8" * 40
        same_content = candidate_identity(
            target_head=TARGET,
            feature_head=FEATURE,
            policy_hash="policy-hash",
            plan_hash=_hash(validation_plan(self.config)),
            rehearsal=repeated,
        )
        self.assertNotEqual(candidate["integrationHead"], same_content["integrationHead"])
        self.assertEqual(candidate["acceptanceTupleHash"], same_content["acceptanceTupleHash"])

    def test_duplicate_reviewer_session_is_blocking(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        replacement = surface_record(
            candidate,
            surface="content-stranger-1",
            verdict="APPROVE",
            reviewer=CONTENT_REVIEWERS["content-stranger-1"],
            session_id=records[0]["sessionId"],
            created_at="2026-08-20T00:01:00Z",
        )
        records[1] = replacement
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("changes_required", result["state"])
        self.assertIn("duplicate-review-session", [item["id"] for item in result["blockers"]])

    def test_agent_record_cannot_escalate_human_authority(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        records[0]["authority"]["mayReblessGolden"] = True
        clean = dict(records[0])
        clean.pop("recordHash")
        records[0]["recordHash"] = __import__("hashlib").sha256(
            json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("collecting", result["state"])
        self.assertEqual("human_authority_escalation", result["staleSurfaceRecords"][0]["reason"])

    def test_same_tuple_later_approval_cannot_mute_blocking_verdict(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        blocking = surface_record(
            candidate,
            surface="content-stranger-1",
            verdict="CHANGES_REQUESTED",
            reviewer=CONTENT_REVIEWERS["content-stranger-1"],
            session_id="blocking-session",
            findings=[{"id": "persistent-blocker", "invariant": "a fix requires a new tuple"}],
            created_at="2026-08-20T00:00:01Z",
        )
        later_approval = surface_record(
            candidate,
            surface="content-stranger-1",
            verdict="APPROVE",
            reviewer=CONTENT_REVIEWERS["content-stranger-1"],
            session_id="later-session",
            created_at="2026-08-20T00:10:00Z",
        )
        records.extend([blocking, later_approval])
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("changes_required", result["state"])
        self.assertIn("persistent-blocker", [item["id"] for item in result["blockers"]])

    def test_content_reviewers_must_be_distinct_even_with_different_sessions(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        records[1] = surface_record(
            candidate,
            surface="content-stranger-1",
            verdict="APPROVE",
            reviewer=records[0]["reviewer"],
            session_id="different-session",
        )
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("collecting", result["state"])
        self.assertEqual("content_reviewer_identity_mismatch", result["staleSurfaceRecords"][0]["reason"])

    def test_all_same_surface_blocking_findings_survive_consolidation(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        for finding_id, created_at in (("blocker-a", "2026-08-20T00:10:00Z"), ("blocker-b", "2026-08-20T00:11:00Z")):
            records.append(
                surface_record(
                    candidate,
                    surface="content-stranger-1",
                    verdict="CHANGES_REQUESTED",
                    reviewer=CONTENT_REVIEWERS["content-stranger-1"],
                    session_id=f"session-{finding_id}",
                    findings=[{"id": finding_id}],
                    created_at=created_at,
                )
            )
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("changes_required", result["state"])
        ids = [item["id"] for item in result["fixBatch"]["findings"]]
        self.assertIn("blocker-a", ids)
        self.assertIn("blocker-b", ids)

    def test_configured_quorum_cannot_be_replaced_by_one_surface_ledger(self) -> None:
        candidate, rehearsal = self.candidate()
        one_surface = evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            required=["content-self"],
            records=[self.records(candidate)[0]],
            repo_root=self.repo_root,
            config=None,
        )
        self.assertEqual("ready", one_surface["state"])
        with self.assertRaisesRegex(HygieneError, "required surface policy mismatch"):
            write_ledger(self.repo_root, self.config, one_surface)

    def test_rehashed_ready_state_with_blockers_is_incoherent_and_finalize_blocks(self) -> None:
        candidate, rehearsal = self.candidate()
        blocked = self.run_evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            records=self.records(candidate, {"content-stranger-1": "CHANGES_REQUESTED"}),
        )
        forged = json.loads(json.dumps(blocked))
        forged["state"] = "ready"
        basis = {key: value for key, value in forged.items() if key not in {"capturedAt", "ledgerHash", "outputPaths"}}
        forged["ledgerHash"] = _hash(basis)
        root = acceptance_root(self.repo_root, self.config)
        history = root / "history" / f"{candidate['acceptanceTupleHash']}-{forged['ledgerHash']}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(forged), encoding="utf-8")
        (root / "latest.json").write_text(json.dumps(forged), encoding="utf-8")
        summary = latest_summary(self.repo_root, self.config)
        self.assertEqual("invalid", summary["state"])
        self.assertIn("coherence mismatch", summary["error"])
        guard = validate_for_finalize(
            self.repo_root,
            self.config,
            {"targetHead": TARGET, "featureHead": FEATURE, "targetBranch": "master", "branch": "feature"},
        )
        self.assertEqual("candidate_acceptance_not_ready", guard["reason"])

    def test_generic_records_cannot_impersonate_hosted_surfaces(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        records[3] = surface_record(
            candidate,
            surface="hosted-tests",
            verdict="APPROVE",
            reviewer="forged-provider",
            session_id="forged-provider-session",
        )
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual("collecting", result["state"])
        self.assertIn("hosted-tests", result["missingSurfaces"])
        self.assertIn("schema_mismatch", [item["reason"] for item in result["staleSurfaceRecords"]])

    def test_provider_repository_and_annotation_shape_are_fail_closed(self) -> None:
        candidate, _ = self.candidate()
        original = json.loads(self.provider_path.read_text(encoding="utf-8"))
        wrong_repo = json.loads(json.dumps(original))
        wrong_repo["repository"] = "someone-else/MLV-App"
        self.write_provider(wrong_repo)
        with self.assertRaisesRegex(HygieneError, "repository"):
            provider_surface_record(
                self.repo_root,
                self.config,
                candidate,
                surface="hosted-tests",
                evidence_path=self.provider_path,
            )
        missing_annotations = json.loads(json.dumps(original))
        codeql = next(run for run in missing_annotations["response"]["check_runs"] if run["name"] == "CodeQL")
        codeql["output"].pop("annotations_count")
        self.write_provider(missing_annotations)
        with self.assertRaisesRegex(HygieneError, "annotation count"):
            provider_surface_record(
                self.repo_root,
                self.config,
                candidate,
                surface="hosted-codeql",
                evidence_path=self.provider_path,
            )

    def test_provider_record_rejects_mutable_non_addressed_evidence_path(self) -> None:
        candidate, _ = self.candidate()
        mutable = self.provider_path.with_name("check-runs.json")
        mutable.write_bytes(self.provider_path.read_bytes())
        with self.assertRaisesRegex(HygieneError, "SHA-addressed"):
            provider_surface_record(
                self.repo_root,
                self.config,
                candidate,
                surface="hosted-tests",
                evidence_path=mutable,
            )

    def test_hosted_verdict_cannot_be_rehashed_against_failing_provider_evidence(self) -> None:
        candidate, rehearsal = self.candidate()
        payload = json.loads(self.provider_path.read_text(encoding="utf-8"))
        codeql = next(run for run in payload["response"]["check_runs"] if run["name"] == "CodeQL")
        codeql["conclusion"] = "failure"
        self.write_provider(payload)
        record = provider_surface_record(
            self.repo_root,
            self.config,
            candidate,
            surface="hosted-codeql",
            evidence_path=self.provider_path,
        )
        self.assertEqual("CHANGES_REQUESTED", record["verdict"])
        record["verdict"] = "APPROVE"
        record["recordHash"] = _hash({key: value for key, value in record.items() if key != "recordHash"})
        ledger = evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            required=["hosted-codeql"],
            records=[record],
            repo_root=self.repo_root,
            config=self.config,
        )
        self.assertEqual("collecting", ledger["state"])
        self.assertEqual("provider_verdict_mismatch", ledger["staleSurfaceRecords"][0]["reason"])

    def test_nonterminal_or_fabricated_provider_state_cannot_finalize(self) -> None:
        candidate, rehearsal = self.candidate()
        ledger = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        live = json.loads(self.provider_path.read_text(encoding="utf-8"))
        product = next(run for run in live["response"]["check_runs"] if run["name"] == "Windows Product Oracles")
        product["status"] = "in_progress"
        product["conclusion"] = None
        with self.assertRaisesRegex(HygieneError, "not terminal"):
            verify_live_provider(self.repo_root, ledger, self.config, payload=live)

        live = json.loads(self.provider_path.read_text(encoding="utf-8"))
        codeql = next(run for run in live["response"]["check_runs"] if run["name"] == "CodeQL")
        codeql["id"] = 999999
        codeql["details_url"] = "https://example.invalid/forged"
        with self.assertRaisesRegex(HygieneError, "drifted"):
            verify_live_provider(self.repo_root, ledger, self.config, payload=live)

    def test_live_provider_query_is_github_pinned_and_bounded(self) -> None:
        provider = json.loads(self.provider_path.read_text(encoding="utf-8"))
        completed = {
            "returncode": 0,
            "timedOut": False,
            "outputCapped": False,
            "cpuStalled": False,
            "stdout": json.dumps(provider["response"]) + "\n200",
            "stderr": "",
        }
        with tempfile.TemporaryDirectory() as client_temp:
            provider_client = Path(client_temp) / ("curl.exe" if os.name == "nt" else "curl")
            provider_client.write_bytes(b"system-curl-client")
            identity = {
                "kind": "os-protected-system-curl",
                "path": str(provider_client.resolve()),
                "size": provider_client.stat().st_size,
                "sha256": hashlib.sha256(b"system-curl-client").hexdigest(),
                "trust": {"ownerSid": "trusted", "unsafeWriteGrants": []},
            }
            with mock.patch.dict(
                os.environ,
                {
                    "GH_HOST": "attacker.invalid",
                    "HTTPS_PROXY": "https://attacker.invalid",
                    "CURL_HOME": "candidate-curl-config",
                    "CURL_CA_BUNDLE": "attacker-ca.pem",
                    "SSL_CERT_FILE": "attacker-ca.pem",
                    "PYTHONPATH": "candidate-python-path",
                    "LD_PRELOAD": "candidate-owned.so",
                    "LD_LIBRARY_PATH": "candidate-libs",
                    "LD_AUDIT": "candidate-audit.so",
                    "DYLD_INSERT_LIBRARIES": "candidate-owned.dylib",
                    "DYLD_LIBRARY_PATH": "candidate-dylibs",
                    "DYLD_FRAMEWORK_PATH": "candidate-frameworks",
                    "DYLD_FALLBACK_LIBRARY_PATH": "candidate-fallback-libs",
                    "DYLD_FALLBACK_FRAMEWORK_PATH": "candidate-fallback-frameworks",
                },
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance.resolve_repo_root",
                return_value=self.repo_root,
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance._trusted_system_curl_identity",
                return_value=identity,
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance._system_curl_path",
                return_value=provider_client.resolve(),
            ), mock.patch(
                "tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process",
                return_value=completed,
            ) as bounded:
                payload = _live_github_provider_payload(
                    self.repo_root,
                    self.config,
                    "layibabalola/MLV-App",
                    FEATURE,
                )
        command = bounded.call_args.args[2]
        self.assertEqual(str(provider_client.resolve()), command[0])
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual("-q", command[1])
        self.assertIn("--proxy", command)
        self.assertEqual("", command[command.index("--proxy") + 1])
        self.assertIn("--noproxy", command)
        self.assertEqual("*", command[command.index("--noproxy") + 1])
        self.assertNotIn("--location", command)
        self.assertEqual(
            f"https://api.github.com/repos/layibabalola/MLV-App/commits/{FEATURE}/check-runs?per_page=100",
            command[-1],
        )
        for key in (
            "GH_HOST", "HTTPS_PROXY", "CURL_HOME", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "PYTHONPATH",
            "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
            "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FALLBACK_FRAMEWORK_PATH",
        ):
            self.assertNotIn(key, bounded.call_args.kwargs["env"])
        self.assertEqual(_minimal_system_child_environment(), bounded.call_args.kwargs["env"])
        self.assertEqual(60000, bounded.call_args.kwargs["timeout_ms"])
        self.assertEqual("candidate-acceptance-github-checks.v1", payload["schema"])
        self.assertEqual("os-protected-system-curl", payload["providerClient"]["kind"])
        self.assertEqual(str(provider_client.resolve()), payload["providerClient"]["path"])
        self.assertEqual(hashlib.sha256(b"system-curl-client").hexdigest(), payload["providerClient"]["sha256"])

    def test_live_github_master_head_is_provider_derived_and_exact(self) -> None:
        response = {
            "ref": "refs/heads/master",
            "object": {"type": "commit", "sha": TARGET},
        }
        with mock.patch(
            "tools.repo_hygiene.candidate_acceptance._live_github_json_payload",
            return_value={"response": response},
        ) as live:
            self.assertEqual(
                live_github_branch_head(
                    self.repo_root,
                    self.config,
                    "layibabalola/MLV-App",
                    "master",
                ),
                TARGET,
            )
        self.assertEqual(
            live.call_args.args[3],
            "https://api.github.com/repos/layibabalola/MLV-App/git/ref/heads/master",
        )
        for mutation in (
            {"ref": "refs/heads/other", "object": {"type": "commit", "sha": TARGET}},
            {"ref": "refs/heads/master", "object": {"type": "tag", "sha": TARGET}},
            {"ref": "refs/heads/master", "object": {"type": "commit", "sha": "short"}},
        ):
            with self.subTest(mutation=mutation), mock.patch(
                "tools.repo_hygiene.candidate_acceptance._live_github_json_payload",
                return_value={"response": mutation},
            ), self.assertRaises(HygieneError):
                live_github_branch_head(
                    self.repo_root,
                    self.config,
                    "layibabalola/MLV-App",
                    "master",
                )

    def test_live_provider_query_ignores_candidate_and_path_executable_shadowing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init")
            fake_gh = repo / ("gh.exe" if os.name == "nt" else "gh")
            fake_python = repo / ("python.exe" if os.name == "nt" else "python")
            fake_gh.write_bytes(b"candidate-gh")
            fake_python.write_bytes(b"candidate-python")
            system_curl = root / ("curl.exe" if os.name == "nt" else "curl")
            system_curl.write_bytes(b"protected-system-curl")
            with mock.patch(
                "tools.repo_hygiene.candidate_acceptance._system_curl_path",
                return_value=system_curl.resolve(),
            ):
                client, command = system_curl_github_query_command(
                    "layibabalola/MLV-App", FEATURE, 4096
                )
            self.assertEqual(system_curl.resolve(), client)
            self.assertEqual(str(client), command[0])
            self.assertNotIn(str(fake_gh), command)
            self.assertNotIn(str(fake_python), command)
            self.assertNotIn("gh", command[:3])
            self.assertNotIn("python", command[:3])

    def test_live_provider_system_curl_identity_must_stay_stable_during_query(self) -> None:
        provider = json.loads(self.provider_path.read_text(encoding="utf-8"))
        completed = {
            "returncode": 0,
            "timedOut": False,
            "outputCapped": False,
            "cpuStalled": False,
            "stdout": json.dumps(provider["response"]) + "\n200",
            "stderr": "",
        }
        with tempfile.TemporaryDirectory() as external_temp:
            provider_client = Path(external_temp) / ("curl.exe" if os.name == "nt" else "curl")
            provider_client.write_bytes(b"before")
            before = {
                "kind": "os-protected-system-curl",
                "path": str(provider_client.resolve()),
                "size": 6,
                "sha256": hashlib.sha256(b"before").hexdigest(),
                "trust": {"unsafeWriteGrants": []},
            }
            after = json.loads(json.dumps(before))
            after["sha256"] = hashlib.sha256(b"after!").hexdigest()
            with mock.patch(
                "tools.repo_hygiene.candidate_acceptance.resolve_repo_root",
                return_value=self.repo_root,
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance._trusted_system_curl_identity",
                side_effect=[before, after],
            ), mock.patch(
                "tools.repo_hygiene.candidate_acceptance._system_curl_path",
                return_value=provider_client.resolve(),
            ), mock.patch(
                "tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process",
                return_value=completed,
            ):
                with self.assertRaisesRegex(HygieneError, "system curl identity changed"):
                    _live_github_provider_payload(
                        self.repo_root,
                        self.config,
                        "layibabalola/MLV-App",
                        FEATURE,
                    )

    def test_live_provider_system_curl_rejects_redirect_status(self) -> None:
        provider = json.loads(self.provider_path.read_text(encoding="utf-8"))
        identity = {
            "kind": "os-protected-system-curl",
            "path": str(Path(os.sys.executable).resolve()),
            "size": Path(os.sys.executable).stat().st_size,
            "sha256": hashlib.sha256(Path(os.sys.executable).read_bytes()).hexdigest(),
            "trust": {"unsafeWriteGrants": []},
        }
        completed = {
            "returncode": 0,
            "timedOut": False,
            "outputCapped": False,
            "cpuStalled": False,
            "stdout": json.dumps(provider["response"]) + "\n302",
            "stderr": "",
        }
        with mock.patch(
            "tools.repo_hygiene.candidate_acceptance.resolve_repo_root",
            return_value=self.repo_root,
        ), mock.patch(
            "tools.repo_hygiene.candidate_acceptance._trusted_system_curl_identity",
            return_value=identity,
        ), mock.patch(
            "tools.repo_hygiene.candidate_acceptance._system_curl_path",
            return_value=Path(os.sys.executable).resolve(),
        ), mock.patch(
            "tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process",
            return_value=completed,
        ):
            with self.assertRaisesRegex(HygieneError, "unexpected HTTP status: 302"):
                _live_github_provider_payload(self.repo_root, self.config, "layibabalola/MLV-App", FEATURE)

    @unittest.skipUnless(os.name == "nt", "Windows ACL/signature trust shape")
    def test_live_provider_system_curl_rejects_user_writable_or_unsigned_client(self) -> None:
        from tools.repo_hygiene.candidate_acceptance import _trusted_system_curl_identity

        with tempfile.TemporaryDirectory() as temp:
            client = Path(temp) / "curl.exe"
            client.write_bytes(b"candidate-controlled-curl")
            base = {
                "returncode": 0,
                "timedOut": False,
                "outputCapped": False,
                "cpuStalled": False,
                "stderr": "",
            }
            unsafe = dict(base, stdout=json.dumps({
                "modulePath": r"C:\Program Files\WindowsPowerShell\Modules;C:\Windows\System32\WindowsPowerShell\v1.0\Modules",
                "loadedModulePaths": [r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"],
                "signatureStatus": "Valid",
                "signerSubject": "CN=Microsoft Windows, O=Microsoft Corporation",
                "signerThumbprint": "A" * 40,
                "pathTrust": [{
                    "path": str(client),
                    "ownerSid": "S-1-5-18",
                    "ownerTrusted": True,
                    "daclPresent": True,
                    "daclNull": False,
                    "unsafeWriteGrants": [{"sid": "S-1-5-4", "rights": "WriteData"}],
                }] * 3,
            }))
            unsigned = dict(base, stdout=json.dumps({
                "modulePath": r"C:\Program Files\WindowsPowerShell\Modules;C:\Windows\System32\WindowsPowerShell\v1.0\Modules",
                "loadedModulePaths": [r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"],
                "signatureStatus": "NotSigned",
                "signerSubject": "",
                "signerThumbprint": "",
                "pathTrust": [{
                    "path": str(client),
                    "ownerSid": "S-1-5-18",
                    "ownerTrusted": True,
                    "daclPresent": True,
                    "daclNull": False,
                    "unsafeWriteGrants": [],
                }] * 3,
            }))
            with mock.patch(
                "tools.repo_hygiene.candidate_acceptance._system_curl_path", return_value=client
            ), mock.patch(
                "tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process",
                side_effect=[unsafe, unsigned],
            ):
                # The refusal names WHICH path grants WHICH principal what, so a failure on an
                # unfamiliar host (an elevated CI token, a non-default C:\ ACL) is diagnosable
                # from its own message instead of needing a re-run under a debugger.
                with self.assertRaisesRegex(
                    HygieneError,
                    r"grants replacement authority.*curl\.exe: S-1-5-4 WriteData",
                ):
                    _trusted_system_curl_identity(self.repo_root, self.config)
                with self.assertRaisesRegex(HygieneError, "valid Microsoft signature"):
                    _trusted_system_curl_identity(self.repo_root, self.config)

    @unittest.skipUnless(os.name == "nt", "Windows ACL token-group classification")
    def test_windows_system_curl_trust_classifies_enabled_group_and_ancestor_replacement(self) -> None:
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with tempfile.TemporaryDirectory() as temp:
            client = Path(temp) / "curl.exe"
            client.write_bytes(b"unsigned-test-client")
            add_rule = (
                _PS_STAGE_TRACE +
                "$ErrorActionPreference='Stop';"
                "Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1');T 'import';"
                "$acl=Get-Acl -LiteralPath $env:MLVAPP_SYSTEM_CURL_PATH;T 'get-acl';"
                "$sid=[System.Security.Principal.SecurityIdentifier]::new('S-1-5-4');"
                "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new("
                "$sid,[System.Security.AccessControl.FileSystemRights]::WriteData,"
                "[System.Security.AccessControl.AccessControlType]::Allow);"
                "$null=$acl.AddAccessRule($rule);T 'add-rule';"
                "Set-Acl -LiteralPath $env:MLVAPP_SYSTEM_CURL_PATH -AclObject $acl;T 'set-acl'"
            )
            env = _minimal_system_child_environment()
            env["MLVAPP_SYSTEM_CURL_PATH"] = str(client)
            # Pinned like production and like the null-DACL fixture, so Get-Acl cannot autoload
            # through a hosted image's large machine PSModulePath (a suspected stall source).
            env["PSModulePath"] = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"
            _run_windows_acl_probe(
                self, "enabled-group fixture: add S-1-5-4 write ACE",
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", add_rule], env=env,
            )
            completed = _run_windows_acl_probe(
                self, "enabled-group fixture: production trust script",
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_CURL_TRUST_SCRIPT],
                env=env,
            )
            trust = json.loads(completed.stdout)
            same_path = lambda value, expected: os.path.normcase(str(Path(value).resolve())) == os.path.normcase(str(expected.resolve()))
            file_trust = next(item for item in trust["pathTrust"] if same_path(item["path"], client))
            self.assertTrue(any(item["sid"] == "S-1-5-4" for item in file_trust["unsafeWriteGrants"]))
            parent_trust = next(item for item in trust["pathTrust"] if same_path(item["path"], client.parent))
            self.assertFalse(parent_trust["ownerTrusted"])
            self.assertTrue(any(item["unsafeWriteGrants"] for item in trust["pathTrust"][:-1]))

    def test_trust_probe_reports_module_evidence_even_when_the_token_grants_replacement(self) -> None:
        # The runner shape, simulated: the protected-module pinning holds but the token (an elevated
        # admin) holds Delete on the real curl path chain. The PROBE (process + module pinning) must
        # succeed so module-shadowing can be tested on any host, while the IDENTITY verdict must
        # still refuse -- the ACL policy is never loosened to make a host pass.
        from tools.repo_hygiene.candidate_acceptance import _trusted_system_curl_identity, _windows_curl_trust_probe

        with tempfile.TemporaryDirectory() as temp:
            client = Path(temp) / "curl.exe"
            client.write_bytes(b"x")
            root = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"
            chain = [
                {"path": p, "ownerSid": "S-1-5-18", "ownerTrusted": True, "daclPresent": True,
                 "daclNull": False, "unsafeWriteGrants": grants}
                for p, grants in (
                    ("C:\\", [{"sid": "S-1-5-32-544", "rights": "FullControl"}]),
                    ("C:\\Windows", [{"sid": "S-1-5-32-544", "rights": "Modify, Synchronize"}]),
                    ("C:\\Windows\\System32", []),
                    (str(client), []),
                )
            ]
            runner = {
                "returncode": 0, "timedOut": False, "outputCapped": False, "cpuStalled": False, "stderr": "",
                "stdout": json.dumps({
                    "modulePath": r"C:\Program Files\WindowsPowerShell\Modules;" + root,
                    "loadedModulePaths": [root + r"\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"],
                    "signatureStatus": "Valid",
                    "signerSubject": "CN=Microsoft Windows, O=Microsoft Corporation",
                    "signerThumbprint": "A" * 40,
                    "pathTrust": chain,
                }),
            }
            with mock.patch(
                "tools.repo_hygiene.candidate_acceptance._system_curl_path", return_value=client
            ), mock.patch(
                "tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process", return_value=runner,
            ):
                trust = _windows_curl_trust_probe(self.repo_root, self.config, client)
                self.assertEqual(root.casefold() + "\\microsoft.powershell.security\\microsoft.powershell.security.psd1",
                                 trust["loadedModulePaths"][0].casefold())
                with self.assertRaisesRegex(
                    HygieneError, r"grants replacement authority.*C:\\: S-1-5-32-544 FullControl"
                ):
                    _trusted_system_curl_identity(self.repo_root, self.config)

    @unittest.skipUnless(os.name == "nt", "Windows token-explained host verdict")
    def test_windows_system_curl_host_verdict_is_explained_by_the_token(self) -> None:
        # The REAL host chain is refused exactly when the current token is an elevated admin (the
        # default Administrators ACEs on C:\ and C:\Windows carry Delete). A hosted runner token is
        # elevated; a normal developer session is not. Any refusal that the elevated-Administrators
        # grant does not explain -- a broad principal, a NULL DACL, another failure -- stays a failure.
        from tools.repo_hygiene.candidate_acceptance import _trusted_system_curl_identity

        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        try:
            _trusted_system_curl_identity(ROOT, self.config)
        except HygieneError as exc:
            message = str(exc)
            self.assertIn("grants replacement authority", message, message)
            flagged = set(re.findall(r": (S-1-[\d-]+|NULL_DACL) ", message))
            self.assertTrue(flagged, message)
            self.assertTrue(elevated, "refused on a NON-elevated token: " + message)
            self.assertEqual({"S-1-5-32-544"}, flagged, message)
        else:
            print(f"[curl-trust] host verdict: clean (elevated={elevated})", file=sys.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows protected-module bootstrap")
    def test_windows_system_curl_trust_rejects_user_module_shadowing(self) -> None:
        from tools.repo_hygiene.candidate_acceptance import _windows_curl_trust_probe, _system_curl_path

        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with tempfile.TemporaryDirectory() as temp:
            attacker_root = Path(temp)
            attacker_module = attacker_root / "Microsoft.PowerShell.Utility"
            attacker_module.mkdir()
            (attacker_module / "Microsoft.PowerShell.Utility.psm1").write_text(
                "function ConvertTo-Json { '{\"forged\":true}' }\n"
                "Export-ModuleMember -Function ConvertTo-Json\n",
                encoding="utf-8",
            )
            hostile_env = _minimal_system_child_environment()
            hostile_env["PSModulePath"] = str(attacker_root)
            baseline = _run_windows_acl_probe(
                self, "module-shadowing baseline",
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "@{safe=$true} | ConvertTo-Json -Compress"],
                env=hostile_env,
            )
            self.assertEqual('{"forged":true}', baseline.stdout.strip())
            # The PROBE is the production process + module-pinning stage; the ACL verdict on the
            # host's real curl chain is asserted separately (host_verdict_is_explained_by_the_token),
            # so this defence is exercised on every host, elevated or not.
            with mock.patch.dict(os.environ, {"PSModulePath": str(attacker_root)}):
                trust = _windows_curl_trust_probe(ROOT, self.config, _system_curl_path())
            self.assertNotIn(str(attacker_root).casefold(), trust["modulePath"].casefold())
            protected_modules = "c:\\windows\\system32\\windowspowershell\\v1.0\\modules\\"
            self.assertTrue(all(str(path).casefold().startswith(protected_modules) for path in trust["loadedModulePaths"]))

    @unittest.skipUnless(os.name == "nt", "Windows NULL-DACL classification")
    def test_windows_system_curl_trust_rejects_null_dacl(self) -> None:
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with tempfile.TemporaryDirectory() as temp:
            client = Path(temp) / "curl.exe"
            client.write_bytes(b"unsigned-null-dacl-test-client")
            env = _minimal_system_child_environment()
            env["MLVAPP_SYSTEM_CURL_PATH"] = str(client)
            env["PSModulePath"] = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"
            null_dacl = (
                _PS_STAGE_TRACE +
                "$ErrorActionPreference='Stop';"
                "Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1');T 'import';"
                "$acl=Get-Acl -LiteralPath $env:MLVAPP_SYSTEM_CURL_PATH;T 'get-acl';"
                "$owner=([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value;"
                "$raw=[Security.AccessControl.RawSecurityDescriptor]::new(('O:{0}G:{0}' -f $owner));"
                "[byte[]]$bytes=New-Object byte[] $raw.BinaryLength;"
                "$raw.GetBinaryForm($bytes,0);$acl.SetSecurityDescriptorBinaryForm($bytes);T 'build-null-dacl';"
                "Set-Acl -LiteralPath $env:MLVAPP_SYSTEM_CURL_PATH -AclObject $acl;T 'set-acl'"
            )
            _run_windows_acl_probe(
                self, "null-dacl fixture: strip DACL",
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", null_dacl], env=env,
            )
            completed = _run_windows_acl_probe(
                self, "null-dacl fixture: production trust script",
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_CURL_TRUST_SCRIPT],
                env=env,
            )
            trust = json.loads(completed.stdout)
            same_path = lambda value: os.path.normcase(str(Path(value).resolve())) == os.path.normcase(str(client.resolve()))
            file_trust = next(item for item in trust["pathTrust"] if same_path(item["path"]))
            self.assertFalse(file_trust["daclPresent"])
            self.assertTrue(file_trust["daclNull"])
            self.assertIn({"sid": "NULL_DACL", "rights": "FullControl"}, file_trust["unsafeWriteGrants"])

    @unittest.skipUnless(os.name != "nt", "Unix fixed-path ownership and ancestry")
    def test_unix_system_curl_trust_rejects_writable_ancestor(self) -> None:
        actual = _trusted_unix_curl_path_chain(Path("/usr/bin/curl"))
        self.assertEqual(["/", "/usr", "/usr/bin", "/usr/bin/curl"], [item["path"] for item in actual])
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "candidate-writable"
            parent.mkdir()
            client = parent / "curl"
            client.write_bytes(b"fake")
            client.chmod(0o555)
            with self.assertRaisesRegex(HygieneError, "root-owned and not group/other writable"):
                _trusted_unix_curl_path_chain(client)

    def test_mandatory_policy_cannot_be_disabled_when_tooling_baseline_is_enforced(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["candidateAcceptance"]["requiredSurfaces"] = ["content-self"]
        guard = validate_for_finalize(self.repo_root, config, {"targetHead": TARGET, "featureHead": FEATURE})
        self.assertEqual("candidate_acceptance_policy_weakened", guard["reason"])

    def test_acceptance_uses_two_phase_target_pinned_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "candidate-acceptance@example.invalid")
            self.git(repo, "config", "user.name", "Candidate Acceptance Test")

            bootstrap = {"contentReviewGate": {"requireClaudeApprovalForFinalize": True}}
            (repo / "closeout.config.json").write_text(json.dumps(bootstrap), encoding="utf-8")
            self.git(repo, "add", "closeout.config.json")
            self.git(repo, "commit", "-m", "pre-infrastructure target")
            pre_infrastructure = self.git(repo, "rev-parse", "HEAD").strip()

            dormant = json.loads(json.dumps(self.config))
            dormant["candidateAcceptance"]["enabled"] = False
            dormant["candidateAcceptance"]["requireReadyForFinalize"] = False
            self.assertFalse(candidate_acceptance_enforced(repo, dormant, {"targetHead": pre_infrastructure}))
            with self.assertRaisesRegex(HygieneError, "must land dormant"):
                candidate_acceptance_enforced(repo, self.config, {"targetHead": pre_infrastructure})

            (repo / "closeout.config.json").write_text(json.dumps(dormant), encoding="utf-8")
            self.git(repo, "add", "closeout.config.json")
            self.git(repo, "commit", "-m", "land dormant infrastructure")
            dormant_target = self.git(repo, "rev-parse", "HEAD").strip()
            self.assertFalse(candidate_acceptance_enforced(repo, dormant, {"targetHead": dormant_target}))
            self.assertTrue(candidate_acceptance_enforced(repo, self.config, {"targetHead": dormant_target}))

            drifted = json.loads(json.dumps(self.config))
            drifted["candidateAcceptance"]["providerRepository"] = "someone-else/MLV-App"
            with self.assertRaisesRegex(HygieneError, "only the two activation booleans"):
                candidate_acceptance_enforced(repo, drifted, {"targetHead": dormant_target})

            (repo / "closeout.config.json").write_text(json.dumps(self.config), encoding="utf-8")
            self.git(repo, "add", "closeout.config.json")
            self.git(repo, "commit", "-m", "activate candidate acceptance")
            active_target = self.git(repo, "rev-parse", "HEAD").strip()
            self.assertTrue(candidate_acceptance_enforced(repo, self.config, {"targetHead": active_target}))
            for enabled, required in ((False, False), (True, False), (False, True)):
                weakened = json.loads(json.dumps(self.config))
                weakened["candidateAcceptance"]["enabled"] = enabled
                weakened["candidateAcceptance"]["requireReadyForFinalize"] = required
                with self.assertRaises(HygieneError):
                    candidate_acceptance_enforced(repo, weakened, {"targetHead": active_target})
            non_boolean = json.loads(json.dumps(self.config))
            non_boolean["candidateAcceptance"]["enabled"] = "true"
            with self.assertRaisesRegex(HygieneError, "must be boolean"):
                candidate_acceptance_enforced(repo, non_boolean, {"targetHead": active_target})
            active_drift = json.loads(json.dumps(self.config))
            active_drift["candidateAcceptance"]["providerRepository"] = "someone-else/MLV-App"
            with self.assertRaisesRegex(HygieneError, "differs from the active pinned target"):
                candidate_acceptance_enforced(repo, active_drift, {"targetHead": active_target})

        config = json.loads(json.dumps(self.config))
        config["candidateAcceptance"]["providerRepository"] = "someone-else/MLV-App"
        guard = validate_for_finalize(self.repo_root, config, {"targetHead": TARGET, "featureHead": FEATURE})
        self.assertEqual("candidate_acceptance_policy_weakened", guard["reason"])

    def test_human_gate_trust_root_is_pinned_to_target_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "candidate-acceptance@example.invalid")
            self.git(repo, "config", "user.name", "Candidate Acceptance Test")
            gate = {
                "requireClaudeApprovalForFinalize": True,
                "coordinationFile": ".claude-state/coordination/gpu-lane-impl-review-sync.md",
                "handoffActor": "CODEX",
                "handoffKind": "HANDOFF",
                "reviewActor": "CLAUDE",
                "reviewKind": "REVIEW",
                "approveTokens": ["APPROVE"],
                "blockingTokens": ["CHANGES_REQUESTED", "BLOCKER"],
                "requireHandoff": True,
                "additionalHandoffActors": ["CLAUDE_IMPL"],
                "authorizedReviewSessions": ["5fc3fc6e-345f-40b8-bb3d-7abd6302b459"],
            }
            baseline = {"contentReviewGate": gate}
            (repo / "closeout.config.json").write_text(json.dumps(baseline), encoding="utf-8")
            self.git(repo, "add", "closeout.config.json")
            self.git(repo, "commit", "-m", "baseline")
            target = self.git(repo, "rev-parse", "HEAD").strip()
            detection = {"targetHead": target}
            self.assertIsNone(content_review_gate_trust_error(repo, baseline, detection))

            for mutation in (
                {"requireClaudeApprovalForFinalize": False},
                {"coordinationFile": ".claude-state/coordination/attacker.md"},
                {"reviewActor": "CANDIDATE"},
                {"authorizedReviewSessions": ["00000000-0000-0000-0000-000000000000"]},
            ):
                changed = json.loads(json.dumps(baseline))
                changed["contentReviewGate"].update(mutation)
                self.assertIn("differs from the pinned target", content_review_gate_trust_error(repo, changed, detection))
                with mock.patch(
                    "tools.repo_hygiene.candidate_acceptance.candidate_acceptance_enforced",
                    return_value=False,
                ):
                    guard = validate_for_finalize(repo, changed, detection)
                self.assertEqual("candidate_acceptance_human_gate_trust_drift", guard["reason"])

            with mock.patch(
                "tools.repo_hygiene.candidate_acceptance.candidate_acceptance_enforced",
                return_value=False,
            ):
                self.assertIsNone(validate_for_finalize(repo, baseline, detection))

    def test_final_integration_tree_or_diff_drift_is_blocking(self) -> None:
        accepted = {"integrationTree": "a" * 40, "diffSha256": "b" * 64}
        self.assertEqual({}, final_integration_mismatches(accepted, dict(accepted)))
        mismatches = final_integration_mismatches(
            accepted,
            {"integrationTree": "c" * 40, "diffSha256": "d" * 64},
        )
        self.assertEqual({"integrationTree", "diffSha256"}, set(mismatches))

    def test_rehearsal_failure_is_batched_after_all_surfaces_terminal(self) -> None:
        candidate, rehearsal = self.candidate(clean=False)
        result = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        self.assertEqual("changes_required", result["state"])
        self.assertEqual("integration-rehearsal-failed", result["fixBatch"]["findings"][0]["id"])

    def test_ledger_hash_is_stable_across_capture_time_and_history_is_immutable(self) -> None:
        candidate, rehearsal = self.candidate()
        records = self.records(candidate)
        with mock.patch("tools.repo_hygiene.candidate_acceptance.utc_now", side_effect=["2026-08-20T00:00:00Z", "2026-08-20T01:00:00Z"]):
            first = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
            second = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=records)
        self.assertEqual(first["ledgerHash"], second["ledgerHash"])
        first_paths = write_ledger(self.repo_root, self.config, first)
        second_paths = write_ledger(self.repo_root, self.config, second)
        self.assertEqual(first_paths["history"], second_paths["history"])
        history = self.repo_root / first_paths["history"]
        self.assertEqual("2026-08-20T00:00:00Z", json.loads(history.read_text(encoding="utf-8"))["capturedAt"])
        self.assertEqual(
            "2026-08-20T01:00:00Z",
            json.loads((self.repo_root / second_paths["latest"]).read_text(encoding="utf-8"))["capturedAt"],
        )

    def test_provider_recapture_preserves_prior_content_addressed_ledger(self) -> None:
        candidate, rehearsal = self.candidate()
        first_provider = self.provider_path
        first = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        write_ledger(self.repo_root, self.config, first)
        recaptured = json.loads(first_provider.read_text(encoding="utf-8"))
        recaptured["capturedAt"] = "2026-08-20T02:00:00Z"
        second_provider = self.write_provider(recaptured)
        self.assertNotEqual(first_provider, second_provider)
        self.assertTrue(first_provider.exists())
        self.assertTrue(second_provider.exists())
        self.assertTrue(latest_summary(self.repo_root, self.config)["ready"])

    def test_monotonic_chain_detects_deleted_same_tuple_blocker_history(self) -> None:
        candidate, rehearsal = self.candidate()
        blocked = self.run_evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            records=self.records(candidate, {"content-stranger-1": "CHANGES_REQUESTED"}),
        )
        paths = write_ledger(self.repo_root, self.config, blocked)
        (self.repo_root / paths["history"]).unlink()
        summary = latest_summary(self.repo_root, self.config)
        self.assertEqual("invalid", summary["state"])
        self.assertIn("history is unreadable", summary["error"])

    def test_same_tuple_blocker_cannot_be_muted_by_direct_writer_or_rehashed_chain(self) -> None:
        candidate, rehearsal = self.candidate()
        blocked = self.run_evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            records=self.records(candidate, {"content-stranger-1": "CHANGES_REQUESTED"}),
        )
        ready = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        write_ledger(self.repo_root, self.config, blocked)
        with self.assertRaisesRegex(HygieneError, "same-tuple blocker is monotonic"):
            write_ledger(self.repo_root, self.config, ready)

        root = acceptance_root(self.repo_root, self.config)
        tuple_hash = candidate["acceptanceTupleHash"]
        ready_history = root / "history" / f"{tuple_hash}-{ready['ledgerHash']}.json"
        ready_history.write_text(json.dumps(ready), encoding="utf-8")
        chain_path = root / "ledger-chain.json"
        chain = json.loads(chain_path.read_text(encoding="utf-8"))
        chain["tuples"][tuple_hash].append(ready["ledgerHash"])
        chain.pop("chainHash")
        chain["chainHash"] = _hash(chain)
        chain_path.write_text(json.dumps(chain), encoding="utf-8")
        (root / "latest.json").write_text(json.dumps(ready), encoding="utf-8")
        summary = latest_summary(self.repo_root, self.config)
        self.assertEqual("invalid", summary["state"])
        self.assertIn("same-tuple blocker is monotonic", summary["error"])

    def test_collecting_ledger_blocker_is_already_monotonic(self) -> None:
        candidate, rehearsal = self.candidate()
        collecting_records = self.records(candidate, {"content-stranger-1": "CHANGES_REQUESTED"})
        collecting_records = [
            record for record in collecting_records if record["surface"] != "content-stranger-2"
        ]
        collecting = self.run_evaluate(
            candidate=candidate,
            rehearsal=rehearsal,
            records=collecting_records,
        )
        self.assertEqual("collecting", collecting["state"])
        self.assertTrue(collecting["blockers"])
        ready = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        write_ledger(self.repo_root, self.config, collecting)
        with self.assertRaisesRegex(HygieneError, "same-tuple blocker is monotonic"):
            write_ledger(self.repo_root, self.config, ready)
        summary = latest_summary(self.repo_root, self.config)
        self.assertEqual("collecting", summary["state"])
        self.assertFalse(summary["ready"])

    def test_state_root_escape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(HygieneError, "must stay under"):
                acceptance_root(Path(temp), {"candidateAcceptance": {"stateRoot": "outside"}})

    def test_latest_summary_is_read_only_and_hash_verified(self) -> None:
        candidate, rehearsal = self.candidate()
        ledger = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        self.assertEqual("missing", latest_summary(self.repo_root, self.config)["state"])
        paths = write_ledger(self.repo_root, self.config, ledger)
        summary = latest_summary(self.repo_root, self.config)
        self.assertTrue(summary["ready"])
        self.assertEqual(candidate["acceptanceTupleHash"], summary["acceptanceTupleHash"])
        latest = self.repo_root / paths["latest"]
        payload = json.loads(latest.read_text(encoding="utf-8"))
        payload["state"] = "changes_required"
        latest.write_text(json.dumps(payload), encoding="utf-8")
        invalid = latest_summary(self.repo_root, self.config)
        self.assertEqual("invalid", invalid["state"])
        self.assertEqual("ledger hash mismatch", invalid["error"])

    def test_finalize_requires_same_tuple_ready_before_content_review(self) -> None:
        candidate, rehearsal = self.candidate()
        ledger = self.run_evaluate(candidate=candidate, rehearsal=rehearsal, records=self.records(candidate))
        detection = {"targetHead": TARGET, "featureHead": FEATURE, "targetBranch": "master", "branch": "feature"}
        self.assertEqual(
            "candidate_acceptance_not_ready",
            validate_for_finalize(self.repo_root, self.config, detection)["reason"],
        )
        write_ledger(self.repo_root, self.config, ledger)
        with mock.patch(
            "tools.repo_hygiene.candidate_acceptance.content_review_gate_trust_error",
            return_value="candidate contentReviewGate differs from the pinned target trust root",
        ):
            trust_guard = validate_for_finalize(self.repo_root, self.config, detection)
        self.assertEqual("candidate_acceptance_human_gate_trust_drift", trust_guard["reason"])
        with mock.patch("tools.repo_hygiene.candidate_acceptance.content_review_gate_trust_error", return_value=None), mock.patch("tools.repo_hygiene.candidate_acceptance._live_github_provider_payload", return_value=json.loads(self.provider_path.read_text(encoding="utf-8"))), mock.patch(
            "tools.repo_hygiene.brokered_closeout.simulate_clean_integration", return_value=rehearsal
        ):
            self.assertIsNone(validate_for_finalize(self.repo_root, self.config, detection))
            drifted = {**detection, "featureHead": "9" * 40}
            guard = validate_for_finalize(self.repo_root, self.config, drifted)
        self.assertEqual(FEATURE, guard["mismatches"]["featureHead"]["actual"])

        source = (ROOT / "tools/repo_hygiene/brokered_closeout.py").read_text(encoding="utf-8")
        finalize = source.split("def _finalize_work_block_once(", 1)[1].split("def finalize_work_block(", 1)[0]
        self.assertLess(
            finalize.index("repair = repair_eligibility"),
            finalize.index("validate_candidate_acceptance_for_finalize"),
        )
        self.assertLess(
            finalize.index("validate_candidate_acceptance_for_finalize"),
            finalize.index("validate_content_review_approval_for_finalize"),
        )
        self.assertIn("candidate_acceptance_final_tree_mismatch", finalize)
        self.assertIn("integration_range_evidence(integration_path, target[\"head\"], merged_head)", finalize)

    def test_tracked_policy_activates_barrier_without_human_authority(self) -> None:
        config = json.loads((ROOT / "closeout.config.json").read_text(encoding="utf-8"))
        policy = config["candidateAcceptance"]
        self.assertTrue(policy["enabled"])
        self.assertTrue(policy["requireReadyForFinalize"])
        self.assertEqual("layibabalola/MLV-App", policy["providerRepository"])
        self.assertTrue(policy["batchUntilAllSurfacesTerminal"])
        self.assertFalse(policy["carryApprovalsAcrossCandidateTuples"])
        self.assertFalse(policy["agentApprovalsGrantHumanAuthority"])

    def test_agent_doctrine_matches_two_phase_acceptance_authority_boundary(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        dashboard = (ROOT / "docs/19-closeout-dashboard-spec.md").read_text(encoding="utf-8")
        self.assertIn("first landing was deliberately dormant while the tooling baseline remained mandatory", agents)
        self.assertIn(
            "separately reviewed activation is now landed: `candidateAcceptance.enabled` and "
            "`candidateAcceptance.requireReadyForFinalize` are both `true`",
            agents,
        )
        self.assertIn("must exactly equal the copy loaded from the pinned target commit in both phases", agents)
        self.assertIn("must never replace that human approval gate", agents)
        self.assertIn("was deliberately dormant (`enabled=false`, `requireReadyForFinalize=false`)", claude)
        self.assertIn("tests, and tooling-baseline inventory are\nalready mandatory", claude)
        self.assertIn("is now active (`enabled=true`,\n`requireReadyForFinalize=true`)", claude)
        self.assertIn("non-authenticating process evidence and never replace", claude)
        self.assertIn("human content-review gate, which remains mandatory in both phases", claude)
        self.assertIn("deliberately dormant first phase", dashboard)
        self.assertIn("separately reviewed activation is now landed with only the two activation\nbooleans enabled", dashboard)
        self.assertIn("must exactly match the\ncopy loaded from the pinned target commit", dashboard)

    def test_every_default_tooling_baseline_path_is_also_tracked(self) -> None:
        """The tooling baseline exists in TWO places and nothing compared them.

        closeout.config.json carries toolingBaseline.paths, and
        brokered_closeout.DEFAULT_CLOSEOUT_CONFIG carries its own copy. Measured 2026-09-05:
        they had already drifted -- tracked held three entries DEFAULT lacked
        (CLOSEOUT-CANONICAL-CONTRACT.md, its .sha256, and closeout_dashboard.py) -- and no test
        looked. Registering a new path in one and not the other would have been silent.

        The invariant asserted is a SUBSET, not equality, and the direction is deliberate.
        Tracked is authoritative and may legitimately be richer than the fallback; but an entry
        present in DEFAULT and missing from tracked means the LIVE policy is weaker than the
        fallback it replaces, which is never intended. Equality would fail today on drift this
        test is not chartered to adjudicate.
        """
        tracked = json.loads((ROOT / "closeout.config.json").read_text(encoding="utf-8"))
        tracked_paths = set(tracked["toolingBaseline"]["paths"])
        default_paths = set(DEFAULT_CLOSEOUT_CONFIG["toolingBaseline"]["paths"])
        missing = sorted(default_paths - tracked_paths)
        self.assertEqual(
            missing,
            [],
            "tooling-baseline paths in DEFAULT_CLOSEOUT_CONFIG but absent from "
            "closeout.config.json: the live policy is weaker than its fallback",
        )

    def test_tracked_and_default_active_policy_and_tooling_guards_stay_in_parity(self) -> None:
        tracked = json.loads((ROOT / "closeout.config.json").read_text(encoding="utf-8"))
        self.assertEqual(tracked["candidateAcceptance"], DEFAULT_CLOSEOUT_CONFIG["candidateAcceptance"])
        self.assertTrue(DEFAULT_CLOSEOUT_CONFIG["candidateAcceptance"]["enabled"])
        self.assertTrue(DEFAULT_CLOSEOUT_CONFIG["candidateAcceptance"]["requireReadyForFinalize"])
        self.assertTrue(tracked["repoSweep"]["evidencePreservingPrune"]["enabled"])
        self.assertTrue(DEFAULT_CLOSEOUT_CONFIG["repoSweep"]["evidencePreservingPrune"]["enabled"])

        tracked_baseline = tracked["toolingBaseline"]
        default_baseline = DEFAULT_CLOSEOUT_CONFIG["toolingBaseline"]
        self.assertEqual(tracked_baseline["requiredTestFiles"], default_baseline["requiredTestFiles"])
        self.assertIn("toolingBaseline.requiredTestFiles", tracked_baseline["requiredConfigKeys"])
        required_paths = {entry["path"] for entry in tracked_baseline["requiredTestFiles"]}
        self.assertEqual(
            {
                "tools/repo_hygiene/test_candidate_acceptance.py",
                "tools/repo_hygiene/test_brokered_closeout.py",
                "tools/repo_hygiene/test_vendored_native_payloads.py",
            },
            required_paths,
        )
        required_symbols = {(entry["path"], entry["contains"]) for entry in tracked_baseline["requiredSymbols"]}
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def integration_range_evidence"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def reusable_candidate_report"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def investigation_jobs"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def fresh_investigation_report"), required_symbols)
        self.assertIn(("tools/repo_hygiene/candidate_acceptance.py", "def validate_for_finalize"), required_symbols)
        self.assertIn(("tools/repo_hygiene/candidate_acceptance.py", "def live_github_issue_comment"), required_symbols)
        self.assertIn(("tools/repo_hygiene/candidate_acceptance.py", "def live_github_branch_head"), required_symbols)
        self.assertIn(("tools/repo_hygiene/vendored_native_payloads.py", "def _verify_provider_owner_claim"), required_symbols)
        self.assertIn(("tools/repo_hygiene/vendored_native_payloads.py", "def _promotion_approval_body"), required_symbols)
        self.assertIn(("tools/repo_hygiene/vendored_native_payloads.py", "def _archive_bytes_snapshot"), required_symbols)
        self.assertIn(("tools/repo_hygiene/vendored_native_payloads.py", "def _load_promotion_claims"), required_symbols)
        self.assertIn(("tools/repo_hygiene/vendored_native_payloads.py", "def _validate_promotion_verifier_unchanged"), required_symbols)
        self.assertIn("repoSweep.maxNewInvestigationsPerRun", tracked_baseline["requiredConfigKeys"])

        current = verify_closeout_tooling_current(ROOT, load_closeout_config(ROOT), attempt_update=False, plan_only=True)
        self.assertTrue(current["ok"], current)
        mutated = load_closeout_config(ROOT)
        mutated["toolingBaseline"]["autoUpdate"] = False
        absent_test = "test_absent_" + "candidate_acceptance_guard"
        mutated["toolingBaseline"]["requiredTestFiles"] = [
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": absent_test}
        ]
        stale = verify_closeout_tooling_current(ROOT, mutated, attempt_update=False, plan_only=True)
        self.assertFalse(stale["ok"], stale)
        self.assertIn(absent_test, {row.get("test") for row in stale["missing"]})

        for label, weaken in (
            (
                "disabled and emptied baseline",
                lambda baseline: baseline.update(
                    {
                        "enabled": False,
                        "paths": [],
                        "requiredConfigKeys": [],
                        "requiredTestFiles": [],
                        "requiredSymbols": [],
                    }
                ),
            ),
            ("omitted baseline enablement", lambda baseline: baseline.pop("enabled", None)),
            ("empty test inventory", lambda baseline: baseline.__setitem__("requiredTestFiles", [])),
            (
                "substituted test path",
                lambda baseline: baseline["requiredTestFiles"][0].__setitem__(
                    "path", "tools/repo_hygiene/test_brokered_closeout.py"
                ),
            ),
            (
                "removed acceptance path",
                lambda baseline: baseline.__setitem__(
                    "paths", [path for path in baseline["paths"] if path != "tools/repo_hygiene/candidate_acceptance.py"]
                ),
            ),
            (
                "removed acceptance symbols",
                lambda baseline: baseline.__setitem__(
                    "requiredSymbols",
                    [
                        item
                        for item in baseline["requiredSymbols"]
                        if item["path"] != "tools/repo_hygiene/candidate_acceptance.py"
                    ],
                ),
            ),
            (
                "removed activation config requirement",
                lambda baseline: baseline.__setitem__(
                    "requiredConfigKeys",
                    [key for key in baseline["requiredConfigKeys"] if key != "candidateAcceptance.enabled"],
                ),
            ),
            (
                "removed bounded investigation config requirement",
                lambda baseline: baseline.__setitem__(
                    "requiredConfigKeys",
                    [key for key in baseline["requiredConfigKeys"] if key != "repoSweep.maxNewInvestigationsPerRun"],
                ),
            ),
            (
                "removed cached-report recomputation symbols",
                lambda baseline: baseline.__setitem__(
                    "requiredSymbols",
                    [
                        item
                        for item in baseline["requiredSymbols"]
                        if item.get("contains")
                        not in {"def reusable_candidate_report", "def investigation_jobs", "def fresh_investigation_report"}
                    ],
                ),
            ),
        ):
            weakened = load_closeout_config(ROOT)
            weakened["toolingBaseline"]["autoUpdate"] = False
            weaken(weakened["toolingBaseline"])
            result = verify_closeout_tooling_current(ROOT, weakened, attempt_update=False, plan_only=True)
            self.assertFalse(result["ok"], f"{label}: {result}")

        for label, raw_acceptance in (
            ("schema removed", {"enabled": False, "requireReadyForFinalize": False}),
            (
                "schema changed",
                {"schema": "candidate-acceptance.v0", "enabled": False, "requireReadyForFinalize": False},
            ),
            ("acceptance object removed", None),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp)
                self.git(repo, "init")
                self.git(repo, "remote", "add", "fork", "https://github.com/layibabalola/MLV-App.git")
                raw = {
                    "toolingBaseline": {
                        "enabled": False,
                        "autoUpdate": False,
                        "paths": [],
                        "requiredConfigKeys": [],
                        "requiredTestFiles": [],
                        "requiredSymbols": [],
                    }
                }
                if raw_acceptance is not None:
                    raw["candidateAcceptance"] = raw_acceptance
                (repo / "closeout.config.json").write_text(json.dumps(raw), encoding="utf-8")
                result = verify_closeout_tooling_current(
                    repo,
                    load_closeout_config(repo),
                    attempt_update=False,
                    plan_only=True,
                )
                self.assertFalse(result["ok"], f"{label}: {result}")
                self.assertIn("baseline_enabled", {row.get("kind") for row in result["missing"]})

        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        # This pin exists to guarantee CI RUNS THIS FILE. It used to assert the exact
        # per-file invocation; the workflow now uses a directory target, which runs it by
        # construction and also catches files added later -- a named list silently omits
        # what it forgets to name. Both halves of the mechanism are pinned, so weakening
        # either the search root or the pattern still fails here.
        self.assertEqual(1, workflow.count("unittest discover -s tools/repo_hygiene"))
        self.assertIn('-p "test_*.py"', workflow)

    def test_github_capture_is_exact_head_terminal_and_fail_closed(self) -> None:
        source = (ROOT / "tools/factory/capture-github-acceptance.ps1").read_text(encoding="utf-8")
        for required in (
            "Ledger feature head does not match HeadSha",
            "Check-run response exceeds the fail-closed single-page bound",
            "Check-run response contains a different head SHA",
            "candidate-acceptance-github-checks.v1",
            "check-runs-$providerHash.json",
            "'record-hosted'",
            "[string]$_.status -cne 'completed'",
            "Repo Hygiene Python (windows-latest)",
            "Windows Product Oracles",
            "Protected Check Route",
            "Analyze (actions)",
            "Analyze (c-cpp)",
            "Analyze (python)",
            "'CodeQL'",
        ):
            self.assertIn(required, source)
        self.assertNotIn("SilentlyContinue", source)
        self.assertNotIn("$env:GH_TOKEN", source)

        python_source = (ROOT / "tools/repo_hygiene/candidate_acceptance.py").read_text(encoding="utf-8")
        self.assertIn('(\"CodeQL\", 57789, True)', python_source)
        self.assertIn('("Windows Product Oracles", 15368, False)', python_source)
        self.assertIn("providerRepository", python_source)
        self.assertIn('"annotations_count" not in output', python_source)
        self.assertIn("hosted surfaces require record-hosted", python_source)
        self.assertIn("def verify_live_provider", python_source)
        self.assertIn("def _trusted_system_curl_identity", python_source)
        self.assertIn("def system_curl_github_query_command", python_source)
        self.assertIn("https://api.github.com", python_source)
        self.assertIn('"-q"', python_source)
        self.assertIn('"--proxy"', python_source)
        self.assertIn('"--noproxy"', python_source)
        self.assertIn('"\\n%{http_code}"', python_source)
        self.assertIn("Get-AuthenticodeSignature", python_source)
        self.assertIn("unsafeWriteGrants", python_source)
        self.assertIn("$principal.IsInRole($sidObject)", python_source)
        self.assertIn("DeleteSubdirectoriesAndFiles", python_source)
        self.assertIn("$PSModuleAutoLoadingPreference = 'None'", python_source)
        self.assertIn("Microsoft.PowerShell.Management\\Get-Item", python_source)
        self.assertIn("Microsoft.PowerShell.Security\\Get-Acl", python_source)
        self.assertIn("Microsoft.PowerShell.Utility\\ConvertTo-Json", python_source)
        self.assertIn("RawSecurityDescriptor", python_source)
        self.assertIn("DiscretionaryAclPresent", python_source)
        self.assertIn("daclNull", python_source)
        self.assertIn("def _minimal_system_child_environment", python_source)
        self.assertIn("def _trusted_unix_curl_path_chain", python_source)
        self.assertIn('"PATH": "/usr/bin:/bin"', python_source)
        self.assertIn('"NoDefaultCurrentDirectoryInExePath": "1"', python_source)
        self.assertIn("run_bounded_closeout_process", python_source)

        command = (
            "$errors=$null; $tokens=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "'tools/factory/capture-github-acceptance.ps1',[ref]$tokens,[ref]$errors) | Out-Null; "
            "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
        )
        powershell = "pwsh.exe" if os.name == "nt" else "pwsh"
        self.assertIsNotNone(shutil.which(powershell), f"required PowerShell executable is unavailable: {powershell}")
        subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=ROOT,
            check=True,
        )

    def test_range_diff_check_catches_whitespace_already_committed_in_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "candidate-acceptance@example.invalid")
            self.git(repo, "config", "user.name", "Candidate Acceptance Test")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-m", "base")
            target = self.git(repo, "rev-parse", "HEAD").strip()
            self.git(repo, "checkout", "-b", "feature")
            (repo / "file.txt").write_text("base\ntrailing whitespace   \n", encoding="utf-8")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-m", "feature")
            feature = self.git(repo, "rev-parse", "HEAD").strip()
            self.git(repo, "checkout", "--detach", target)
            config = {"stateRoot": ".claude-state/closeout", "validation": {"commands": []}}
            result = simulate_clean_integration(
                repo,
                config,
                target_head=target,
                branch_head=feature,
                branch_name="feature",
                target_branch="master",
            )
            self.assertFalse(result["clean"])
            self.assertEqual("diff_check_failed", result["reason"])
            self.assertIn("file.txt", result["stdout"])

    def test_diff_identity_is_independent_of_git_color_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.git(repo, "init")
            self.git(repo, "config", "user.email", "candidate-acceptance@example.invalid")
            self.git(repo, "config", "user.name", "Candidate Acceptance Test")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-m", "base")
            target = self.git(repo, "rev-parse", "HEAD").strip()
            self.git(repo, "checkout", "-b", "feature")
            (repo / "file.txt").write_text("base\nfeature\n", encoding="utf-8")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-m", "feature")
            feature = self.git(repo, "rev-parse", "HEAD").strip()
            self.git(repo, "checkout", "--detach", target)
            config = {"stateRoot": ".claude-state/closeout", "validation": {"commands": []}}
            normal = simulate_clean_integration(
                repo,
                config,
                target_head=target,
                branch_head=feature,
                branch_name="feature",
                target_branch="master",
            )
            self.git(repo, "config", "color.ui", "always")
            self.git(repo, "config", "diff.context", "10")
            self.git(repo, "config", "diff.indentHeuristic", "true")
            colored = simulate_clean_integration(
                repo,
                config,
                target_head=target,
                branch_head=feature,
                branch_name="feature",
                target_branch="master",
            )
            self.assertTrue(normal["clean"])
            self.assertTrue(colored["clean"])
            self.assertEqual(normal["diffSha256"], colored["diffSha256"])
            self.assertEqual(normal["integrationTree"], colored["integrationTree"])

    @staticmethod
    def git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return completed.stdout


class WindowsAclProbeTimeoutTests(unittest.TestCase):
    """A stalled powershell.exe ACL probe is UNKNOWN and fails; it never passes or vanishes quietly."""

    COMMAND = ["powershell.exe", "-Command", "x"]

    def _stalled(self, *, stdout: object = b"", stderr: object = b"stage get-acl 41ms"):
        return mock.patch(
            "tools.repo_hygiene.test_candidate_acceptance.subprocess.run",
            side_effect=subprocess.TimeoutExpired(self.COMMAND, 3, output=stdout, stderr=stderr),
        )

    def test_a_timeout_fails_as_typed_unknown_with_partial_output(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop(ALLOW_UNKNOWN_ACL_PROBE_ENV, None)
            with self._stalled(), self.assertRaises(WindowsAclProbeUnknown) as ctx:
                _run_windows_acl_probe(self, "fixture", self.COMMAND, env={}, timeout=3)
        message = str(ctx.exception)
        self.assertIn("UNKNOWN", message)
        self.assertIn("NOT checked", message)
        self.assertIn("stage get-acl 41ms", message)
        self.assertIsInstance(ctx.exception, AssertionError)  # reported as a failure, not a skip

    def test_the_named_flag_skips_only_when_set_and_logs_the_skip(self) -> None:
        for value in ("0", "", "true"):
            with mock.patch.dict(os.environ, {ALLOW_UNKNOWN_ACL_PROBE_ENV: value}):
                with self._stalled(), self.assertRaises(WindowsAclProbeUnknown):
                    _run_windows_acl_probe(self, "fixture", self.COMMAND, env={}, timeout=3)
        log = io.StringIO()
        with mock.patch.dict(os.environ, {ALLOW_UNKNOWN_ACL_PROBE_ENV: "1"}), mock.patch("sys.stderr", log):
            with self._stalled(), self.assertRaises(unittest.SkipTest):
                _run_windows_acl_probe(self, "fixture", self.COMMAND, env={}, timeout=3)
        self.assertIn("SKIPPED-UNKNOWN", log.getvalue())
        self.assertIn(ALLOW_UNKNOWN_ACL_PROBE_ENV, log.getvalue())

    def test_a_probe_that_returns_is_passed_through(self) -> None:
        done = subprocess.CompletedProcess(self.COMMAND, 0, stdout="{}", stderr="")
        with mock.patch("tools.repo_hygiene.test_candidate_acceptance.subprocess.run", return_value=done):
            self.assertIs(done, _run_windows_acl_probe(self, "fixture", self.COMMAND, env={}, timeout=3))

    def test_no_workflow_sets_the_skip_flag(self) -> None:
        offenders = [
            path.name for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
            if ALLOW_UNKNOWN_ACL_PROBE_ENV in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], offenders, "CI must never enable the UNKNOWN-probe skip")


if __name__ == "__main__":
    unittest.main()
