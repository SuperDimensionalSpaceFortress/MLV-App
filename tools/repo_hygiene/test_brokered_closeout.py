import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from jsonschema import Draft202012Validator

from .brokered_closeout import (
    DEFAULT_CLOSEOUT_CONFIG,
    prune_worktrees,
    stale_locked_worktree_admin_entries,
    bounded_closeout_run,
    bounded_runner_exit_code,
    bounded_runner_exit_codes,
    agent_remediation_queue_consumer_plan,
    agent_remediation_dirty_state_hash,
    bootstrap_response_broker_manifest,
    broker_contract,
    check_review_quorum,
    checkpoint_owned_work,
    checkpoint_commit_message,
    closeout_clean_truth_from_postcondition,
    coordination_entries,
    coordination_entry_actor_kind,
    coordination_entry_matches,
    coordination_entry_seat_session,
    validate_content_review_approval_for_finalize,
    closeout_merge_commit_message,
    closeout_script_command,
    delivered_work_commit_subject,
    dirty_split_commit_message,
    enter_remediation_freeze,
    evidence_repair_commit_message,
    effective_closeout_script_command,
    copy_exact_path_for_split,
    collect_agent_remediation_results,
    closeout_command_timeout_ms,
    closeout_max_process_output_bytes,
    closeout_process_resource_policy,
    complete_work_block,
    canonical_worktree_root,
    detect_work_block,
    finalize_action_id,
    finalize_candidate_id,
    finalize_evidence,
    finalize_retry_decision,
    finalize_work_block,
    guard_closeout_hook,
    load_closeout_config,
    load_manifest,
    file_content_hash,
    plan_dirty_split_candidates,
    apply_dirty_split_candidate,
    apply_detached_dirty_preserve,
    preserve_owned_dirty_split,
    record_review_approval,
    repair_missing_evidence,
    repair_target_push_failure,
    run_git,
    repair_eligibility,
    remediate_retained_candidates,
    retire_stale_plan_absent_agent_queue_packets,
    repo_sweep,
    repo_sweep_plan,
    repo_sweep_tuple,
    repo_state_snapshot,
    powershell_policy,
    powershell_executable_for_policy,
    restart_runtime_services_after_clean_promotion,
    run_bounded_closeout_process,
    run_validations,
    validation_full_suite_requested,
    remediation_freeze_status,
    remediation_freeze_staleness,
    remove_remediation_freeze,
    remove_worktree,
    remediation_packet_template,
    stable_hash,
    start_work_block,
    stop_runtime_services_before_promotion,
    target_worktree_update_preflight,
    update_local_target,
    delete_remote_feature_ref,
    worktree_dirty_state,
    validate_rollback_manifest,
    verify_repo_closed_postcondition,
    verify_prune_recovery_artifact,
    write_audit,
    write_review_surface_unavailable_report,
)
from .closeout import validate_compare_result_schema
from .closeout_dashboard import (
    MAX_DASHBOARD_ACTION_REQUESTS,
    dashboard_action_preview_payload,
    dashboard_action_request_payload,
    dashboard_action_request_history_payload,
    dashboard_actions_payload,
    dashboard_html,
    history_index_payload,
    history_snapshot_payload,
    latest_repo_state_payload,
)
from .core import HygieneError


ROOT = Path(__file__).resolve().parents[2]

FROZEN_CLOSEOUT_CAPABILITY_ROWS = [
    "historical-incident-traceability",
    "requirements-trace-to-original-standard",
    "capability-ledger-schema",
    "capability-ledger-populated",
    "structured-adjudication-protocol",
    "declared-review-surface",
    "candidate-evidence-packet",
    "adjudication-report",
    "adjudication-to-symbolic-action-boundary",
    "repo-owned-mutation-after-adjudication",
    "exact-mutation-tuple",
    "bounded-wrapper-authority",
    "broker-manifest-dirty-baseline",
    "deterministic-work-block-selection",
    "dirty-classification",
    "foreign-dirty-preservation",
    "baseline-dirty-mixed-path-protection",
    "hard-clean-final-gate",
    "repo-closed-for-final-response",
    "advisory-hooks-non-authoritative",
    "response-hook-no-worktree-resurrection",
    "final-utility-generated-or-preclean",
    "tooling-drift-detection",
    "repo-sweep-read-only-planning",
    "repo-sweep-single-candidate-mutation",
    "audited-bulk-override",
    "no-force-push-target-recovery",
    "local-only-repo-closeout",
    "clean-standard-non-smuggling",
    "protected-target-noop",
    "target-push-race-recovery",
    "retained-candidate-remediation",
    "surface-unavailable-or-insufficient-reviewer-block",
    "checked-out-and-locked-worktree-handling",
    "independent-review-quorum",
    "git-hook-gates",
    "agent-remediation-queue",
    "automated-subagent-dispatch",
    "evidence-preserving-prune",
    "remediation-freeze",
    "dirty-split-automation",
    "runtime-service-lifecycle",
    "remote-feature-clean-integration",
    "retained-candidate-auto-closeout-remediation",
]


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True)
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def deep_update(base: dict, updates: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_update(result[key], value)
        else:
            result[key] = value
    return result


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            capture_output=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# Building the base repo costs seven `git` child processes (ten with a remote),
# and `init_repo()` has 212 call sites here.  On Windows a spawn is ~30ms against
# ~3-5ms on Linux, which is why this module runs 448.8s on the hosted Windows leg
# versus 40.7s on ubuntu (run 35242441426).  One template per distinct
# (config, remote) shape is built once per process and copied per test.  A git
# repository is self-contained, so the copy is exact; only `origin`'s absolute
# URL has to be re-pointed afterwards.
#
# Measured A,B,A,B on Windows: 578/552s baseline vs 527/450s here, 249/249 passing
# in every arm.  Consistently faster, but only ~9-18% -- the fixture is a slice,
# not the cause.  The module makes ~17,800 git spawns in total, ~7,300 of them
# `git rev-parse`; caching resolved refs is where the rest of the time is.
# See .claude-state/project-memory/windows-ci-5x-gap-root-cause-20260917.md.
_TEMPLATE_CACHE: dict[tuple[str, bool], Path] = {}
_TEMPLATE_ROOT: Path | None = None


def _template_root() -> Path:
    global _TEMPLATE_ROOT
    if _TEMPLATE_ROOT is None:
        _TEMPLATE_ROOT = Path(tempfile.mkdtemp(prefix="brokered-closeout-template-"))
        atexit.register(shutil.rmtree, _TEMPLATE_ROOT, True)
    return _TEMPLATE_ROOT


class BrokeredCloseoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="brokered-closeout-test-"))
        self.repo_counter = 0
        self.acceptance_patcher = mock.patch(
            "tools.repo_hygiene.candidate_acceptance.validate_for_finalize",
            return_value=None,
        )
        self.acceptance_patcher.start()
        self.addCleanup(self.acceptance_patcher.stop)
        self.acceptance_enforcement_patcher = mock.patch(
            "tools.repo_hygiene.candidate_acceptance.candidate_acceptance_enforced",
            return_value=False,
        )
        self.acceptance_enforcement_patcher.start()
        self.addCleanup(self.acceptance_enforcement_patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def write_config(self, repo: Path, updates: dict | None = None) -> None:
        config = {
            "stateRoot": ".claude-state/closeout",
            "git": {
                "targetBranch": "master",
                "remote": "origin",
                "allowLocalOnly": True,
                "protectedBranches": ["master", "main"],
                "featureBranchPatterns": ["codex/*", "feature/*"],
                "fetchBeforeEvidence": True,
            },
            "workBlockBootstrap": {
                "autoBranchFromProtectedTarget": True,
                "branchPrefix": "codex/work-block",
            },
            "validation": {"commands": []},
            "candidateAcceptance": {
                "enabled": False,
                "requireReadyForFinalize": False,
            },
            "paths": {
                "generated": [".claude-state/**", ".codex-state/**", "**/__pycache__/**", "**/*.pyc"],
                "sensitive": [".claude/**", ".claude", ".git/**", ".git"],
                "state": ".claude-state/closeout",
            },
            "dirty": {
                "unclaimedOutsideDelta": "foreign",
                "sensitiveUnownedBlocks": True,
                "autoClaimCleanAtStart": True,
                "cleanAtStartSensitiveAutoClaim": [".claude/analysis/*.md", ".claude/ANALYSIS_LOG.md"],
            },
            "dirtySplit": {
                "enabled": True,
                "autoRepairOwnedDirty": True,
                "autoCheckpointOwnedDirty": True,
                "branchPrefix": "closeout/split",
                "worktreeRoot": ".claude-state/closeout/dirty-splits/worktrees",
                "maxCandidatesPerRun": 1,
                "registerBrokerOwnership": True,
            },
            "responseHookLifecycle": {
                "skipSessionWorktreeSignal": "SkipSessionWorktree",
                "skipAuditField": "session_worktree_bootstrap",
                "readOnlyHookPhases": ["response", "final"],
                "managedSessionWorktreeRoots": [".codex-worktrees/**"],
                "bootstrapAllowedOnlyByExplicitStart": True,
            },
            "hardClean": {
                "requireForCompletion": True,
                "allowRetainedForeignDirtyAtCompletion": True,
                "requireNoStash": True,
                "requireWorktreeInspection": True,
                "requireNoLinkedSiblingWorktrees": True,
                "protectedTargetNoopCloseout": {"enabled": True},
                "unifiedTruthReport": {
                    "enabled": True,
                    "authoritativeSource": "repoClosedPostcondition.closeoutCleanTruth",
                    "reportFields": ["rawGit", "policy", "cleanup"],
                },
                "exemptStatusPatterns": [".claude-state/**", ".codex-state/**", "**/__pycache__/**", "**/*.pyc"],
            },
            "runtimeServices": {
                "mlvapp-preview": {
                    "enabled": False,
                    "stopBeforePromotion": True,
                    "restartAfterCleanPromotion": False,
                    "statusCommand": [],
                    "stopCommand": [],
                    "startCommand": [],
                }
            },
            "toolingBaseline": {"enabled": False},
            "locking": {
                "detectTimeoutMs": 120000,
                "finalizeTimeoutMs": 600000,
                "repoSweepTimeoutMs": 600000,
                "maxProcessOutputBytes": 1048576,
                "failureTextPatterns": ["closeout gate failure", "review quorum failure", "review_quorum_missing"],
            },
            "autoEligibilityRepair": {"timeoutMs": 300000},
            "remediationFreeze": {
                "enabled": True,
                "markerPath": ".claude-state/closeout-remediation.freeze",
                "envVar": "CLOSEOUT_REMEDIATION_FREEZE",
                "generatedAuditRoot": ".claude-state/closeout-log/remediation-freeze",
                "clusterLedgerRoot": ".claude-state/closeout-log/remediation-clusters",
                "requireCoordinatorLock": True,
                "requireExactAllowlist": True,
                "requireRecoveryBundle": True,
                "requireRemoteAdvertisedPins": True,
                "requireHookGuardProof": True,
                "requiredReviewerScore": 10,
                "requiredReviewers": ["codex-self", "stranger-reviewer-1", "stranger-reviewer-2"],
                "blockedLifecycleActions": ["broker-bootstrap", "start-work-block", "publish", "finalize", "pre-commit", "pre-push", "response-hook", "final-hook"],
            },
            "evidenceRepair": {
                "enabled": False,
                "evidenceRoot": ".closeout-evidence",
                "requiredArtifacts": ["metrics.json", "handoff.json", "session.json", "closeout.json"],
                "requiredFor": ["publish_missing_upstream", "publish_ahead_only", "final_push"],
                "commitMessage": "chore(closeout): repair closeout evidence artifacts",
            },
            "reviewQuorum": {
                "requiredApprovals": 3,
                "requiredScore": 10,
                "requiredSelfApprovals": 1,
                "requiredIndependentApprovals": 2,
                "selfReviewers": ["codex-self"],
                "independentReviewers": ["ancestry-safety-reviewer", "mutation-scope-reviewer"],
                "allowedReviewers": ["local-test", "codex-self", "ancestry-safety-reviewer", "mutation-scope-reviewer"],
                "highImpactActions": ["clean_integrate", "checkpoint-owned-dirty", "delete_local_branch", "delete_remote_branch", "repo_sweep_prune_merged", "split", "resolve_conflicts_with_agent", "preserve_dirty_cluster", "release_stale_claim", "remove_remediation_freeze"],
                "tupleFields": ["candidateId", "actionId", "evidenceHash", "policyHash", "pinnedRefs"],
            },
            "agentRemediation": {
                "enabled": True,
                "queueRoot": ".claude-state/closeout/agent-remediation",
                "maxConflictFilesPerAgent": 12,
                "maxChangedPathsPerAgent": 250,
                "requireSurfaceExecution": True,
                "surfaceAdapters": ["codex-desktop", "claude-desktop-command-bridge"],
                "conflictPathGroups": {
                    "agent-policy": ["AGENTS.md", "CLAUDE.md", ".claude/**", "docs/**"],
                    "qt-playback": ["platform/qt/**"],
                    "mlv-core": ["src/mlv/**", "src/processing/**", "src/debayer/**"],
                    "batch-debug": ["src/batch/**", "src/debug/**"],
                    "tests": ["tests/**", ".github/**"],
                },
            },
            "agentRemediationQueue": {
                "enabled": True,
                "queueRoots": [".claude-state/closeout/agent-remediation"],
                "resultRoot": ".claude-state/closeout/agent-remediation/results",
                "maxParallelAgents": 3,
                "perAgentTimeoutMs": 600000,
                "maxAgentOutputBytes": 1048576,
                "requireExactTuple": True,
                "surfaceUnavailableStatus": "agent_remediation_surface_unavailable",
            },
            "autoQuorum": {
                "enabled": True,
                "requiredScore": 10,
                "allowStaleReviewRenewal": True,
                "reviewers": ["codex-self", "ancestry-safety-reviewer", "mutation-scope-reviewer"],
                "autonomousActionClasses": [
                    "integrated_branch_prune",
                    "integrated_remote_feature_prune",
                    "patch_equivalent_remote_feature_prune",
                    "remote_feature_clean_integrate",
                    "repo_sweep_clean_integrate",
                    "owned_dirty_checkpoint",
                    "stale_locked_worktree_cleanup",
                    "redundant_backup_prune",
                    "dirty_split",
                    "foreign_dirty_integrated_branch_prune",
                    "detached_dirty_preserve",
                    "generated_only_detached_worktree_prune",
                    "redundant_branch_prune",
                    "explicit_protected_worktree_cleanup",
                    "agent_conflict_remediation",
                    "dirty_cluster_preservation",
                    "stale_claim_remediation",
                    "remediation_freeze_removal",
                ],
                "manualOnlyActionClasses": ["protected_branch", "dirty_worktree", "locked_worktree", "ambiguous_merge_required", "active_locked_worktree", "unowned_dirty_triage"],
            },
            "repoSweep": {
                "enabled": True,
                "mergeMode": "auto_clean",
                "pruneMergedLocalBranches": True,
                "pruneWorktrees": "clean_detached_only",
                "stashMode": "retain",
                "agentDispatchMode": "deterministic",
                "investigateRetainedCandidates": True,
                "retainedBlockerAutoRemediation": {"enabled": True},
                "recoveryBranchPrefix": "closeout/recovery/detached",
                "allowForeignDirtyIntegratedBranchPrune": True,
                "allowPatchEquivalentPrune": True,
                "protectedLockedWorktreeExactPolicy": [],
                "allowCleanCheckedOutIntegration": True,
                "allowStaleLockedWorktreeCleanup": True,
                "lockedWorktreeStaleHours": 24,
                "backupBranchPatterns": ["*backup*", "backup/*", "*-backup", "*-backup-*"],
                "detachedDirtyGeneratedOnlyPatterns": [
                    ".qmake.stash",
                    "Makefile",
                    "Makefile.*",
                    "moc/**",
                    "moc_*.cpp",
                    "qrc_*.cpp",
                    "ui/**",
                    "ui_*.h",
                    "MLVApp_resource.rc",
                    "obj/**",
                    "*.log",
                ],
                "fetchBeforeRemoteSweep": True,
                "remoteFeaturePatterns": [],
                "pruneRemoteFeatureBranches": True,
                "cleanIntegrateRemoteFeatureBranches": True,
                "deleteRemoteFeatureAfterCleanIntegrate": True,
                "evidencePreservingPrune": {
                    "enabled": True,
                    "recoveryRoot": ".claude-state/closeout/manual-prune",
                    "requireBundleForNonAncestor": True,
                    "requireDirtyWorktreeBytePreservation": True,
                    "requireReviewerVerdicts": True,
                    "rerunSweepAfterPrune": True,
                },
            },
            "blockerAutoRemediation": {
                "enabled": True,
                "allowForeignDirtyIntegratedBranchSwitch": True,
                "allowDetachedDirtyPreservation": True,
                "allowSensitiveDetachedDirtyPreservation": False,
                "allowLargeDetachedDirtyPreservation": True,
                "maxDetachedDirtyPaths": 25,
                "prunePatchEquivalentBranches": True,
                "maxConflictFilesForAgent": 8,
                "explicitProtectedWorktreeActions": [],
            },
        }
        if updates:
            config = deep_update(config, updates)
        (repo / "closeout.config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    def _build_repo_template(
        self, destination: Path, *, remote: bool, config_updates: dict | None
    ) -> None:
        """Build the canonical base repo exactly as this fixture always has."""
        repo = destination / "repo"
        repo.mkdir(parents=True)
        git(repo, "init", "-b", "master")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test User")
        (repo / ".gitignore").write_text(".claude-state/\n", encoding="utf-8")
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        (repo / "foreign.txt").write_text("base foreign\n", encoding="utf-8")
        git(repo, "add", ".gitignore", "README.md", "foreign.txt")
        git(repo, "commit", "-m", "initial")
        self.write_config(repo, config_updates)
        git(repo, "add", "closeout.config.json")
        git(repo, "commit", "-m", "add closeout policy")
        if remote:
            bare = destination / "origin.git"
            git(destination, "init", "--bare", str(bare))
            git(repo, "remote", "add", "origin", str(bare))
            git(repo, "push", "-u", "origin", "master")

    def _repo_template(self, *, remote: bool, config_updates: dict | None) -> Path:
        key = (json.dumps(config_updates, sort_keys=True, default=str), remote)
        template = _TEMPLATE_CACHE.get(key)
        if template is None:
            digest = hashlib.sha256(
                f"{key[0]}|{remote}".encode("utf-8")
            ).hexdigest()[:16]
            template = _template_root() / digest
            self._build_repo_template(
                template, remote=remote, config_updates=config_updates
            )
            _TEMPLATE_CACHE[key] = template
        return template

    def init_repo(self, *, remote: bool = False, config_updates: dict | None = None) -> Path:
        self.repo_counter += 1
        repo = self.tempdir / ("repo" if self.repo_counter == 1 else f"repo-{self.repo_counter}")
        template = self._repo_template(remote=remote, config_updates=config_updates)
        shutil.copytree(template / "repo", repo)
        if remote:
            bare = self.tempdir / f"origin-{self.repo_counter}.git"
            shutil.copytree(template / "origin.git", bare)
            # The template's origin URL points into the template root; re-point
            # it at this test's own bare copy.
            git(repo, "remote", "set-url", "origin", str(bare))
        return repo

    def make_feature(self, repo: Path, work_block_id: str, *, filename: str = "work.txt") -> dict:
        git(repo, "checkout", "-b", "codex/test-work")
        block = start_work_block(repo, work_block_id=work_block_id, actor="local-test", path_claims=[filename])
        (repo / filename).write_text("feature work\n", encoding="utf-8")
        git(repo, "add", filename)
        git(repo, "commit", "-m", "feature work")
        return block

    def test_start_work_block_auto_branches_from_clean_protected_target(self) -> None:
        repo = self.init_repo()

        result = start_work_block(repo, work_block_id="wb-protected-start", actor="local-test")

        branch = "codex/work-block/wb-protected-start"
        self.assertEqual(result["status"], "started", result)
        self.assertEqual(git(repo, "branch", "--show-current").stdout.strip(), branch)
        manifest = result["manifest"]
        self.assertEqual(manifest["branch"], branch)
        self.assertEqual(manifest["workBlockId"], "wb-protected-start")
        self.assertEqual(manifest["dirtyBaseline"]["paths"], [])
        self.assertEqual(manifest["protectedBranchBootstrap"]["fromProtectedBranch"], "master")
        self.assertEqual(manifest["protectedBranchBootstrap"]["createdBranch"], branch)
        self.assertEqual(manifest["protectedBranchBootstrap"]["reason"], "protected_branch_auto_branch")
        self.assertEqual(manifest["startHead"], manifest["protectedBranchBootstrap"]["startHead"])
        manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-protected-start" / "manifest.json"
        self.assertTrue(manifest_path.exists())

    def test_start_work_block_records_human_commit_subject(self) -> None:
        repo = self.init_repo()

        result = start_work_block(
            repo,
            work_block_id="wb-subject-start",
            actor="local-test",
            summary="closeout: require human commit subjects",
        )

        self.assertEqual(result["manifest"]["commitSubject"], "closeout: require human commit subjects")
        manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-subject-start" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["commitSubject"], "closeout: require human commit subjects")

    def test_start_work_block_can_record_target_contained_start_head(self) -> None:
        repo = self.init_repo(remote=False)
        base_head = git(repo, "rev-parse", "master").stdout.strip()
        git(repo, "checkout", "-b", "codex/target-base-start")
        (repo / "work.txt").write_text("feature work\n", encoding="utf-8")
        git(repo, "add", "work.txt")
        git(repo, "commit", "-m", "feature work")
        feature_head = git(repo, "rev-parse", "HEAD").stdout.strip()

        result = start_work_block(
            repo,
            work_block_id="wb-target-base-start",
            actor="local-test",
            start_head="master",
        )

        manifest = result["manifest"]
        self.assertEqual(manifest["startHead"], base_head)
        self.assertEqual(manifest["startHeadOverride"]["requested"], "master")
        self.assertEqual(manifest["startHeadOverride"]["resolved"], base_head)
        self.assertEqual(manifest["startHeadOverride"]["currentHead"], feature_head)
        self.assertEqual(manifest["startHeadOverride"]["targetHead"], base_head)

    def test_start_work_block_rejects_mid_lane_start_head_override(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/mid-lane-start")
        (repo / "one.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "one.txt")
        git(repo, "commit", "-m", "feature one")
        mid_lane_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "two.txt").write_text("two\n", encoding="utf-8")
        git(repo, "add", "two.txt")
        git(repo, "commit", "-m", "feature two")

        with self.assertRaisesRegex(HygieneError, "contained in target"):
            start_work_block(
                repo,
                work_block_id="wb-mid-lane-start",
                actor="local-test",
                start_head=mid_lane_head,
            )

    def test_start_work_block_rejects_generic_commit_subject(self) -> None:
        repo = self.init_repo()

        with self.assertRaisesRegex(HygieneError, "work block summary must be a human-readable commit subject"):
            start_work_block(repo, work_block_id="wb-generic-subject", actor="local-test", summary="brokered closeout checkpoint")

    def test_start_work_block_blocks_dirty_protected_target_before_auto_branch(self) -> None:
        repo = self.init_repo()
        (repo / "dirty.txt").write_text("dirty target state\n", encoding="utf-8")

        with self.assertRaisesRegex(HygieneError, "cannot auto-branch from protected branch master"):
            start_work_block(repo, work_block_id="wb-dirty-protected-start", actor="local-test")

        self.assertEqual(git(repo, "branch", "--show-current").stdout.strip(), "master")
        manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-dirty-protected-start" / "manifest.json"
        self.assertFalse(manifest_path.exists())

    def approve_current_tuple(self, repo: Path, work_block_id: str) -> dict:
        config = load_closeout_config(repo)
        detection = detect_work_block(repo, work_block_id=work_block_id)
        evidence = finalize_evidence(config, detection)
        evidence_hash = stable_hash(evidence)
        return record_review_approval(
            repo,
            candidate_id=finalize_candidate_id(work_block_id),
            action_id=finalize_action_id(),
            evidence_hash=evidence_hash,
            pinned_refs=detection["pinnedRefs"],
            reviewer="local-test",
            approved=True,
        )

    def write_gpu_lane_coordination(self, repo: Path, text: str) -> Path:
        path = repo / ".claude-state" / "coordination" / "gpu-lane-impl-review-sync.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def audit_types(self, repo: Path) -> list[str]:
        audit_log = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        if not audit_log.exists():
            return []
        return [json.loads(line)["auditType"] for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def audit_rows(self, repo: Path) -> list[dict]:
        audit_log = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        if not audit_log.exists():
            return []
        return [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write_closeout_work_block_manifest(self, repo: Path, work_block_id: str = "wb-rollback") -> None:
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        manifest = {
            "schemaVersion": "1.0",
            "workBlockId": work_block_id,
            "state": "completed",
            "actor": "local-test",
            "branch": git(repo, "branch", "--show-current").stdout.strip(),
            "worktree": str(repo),
            "targetBranch": "master",
            "pathClaims": [],
            "startHead": head,
            "dirtyBaseline": {"paths": [], "entries": []},
            "startedAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        }
        block_dir = repo / ".claude-state" / "closeout" / "work-blocks" / work_block_id
        block_dir.mkdir(parents=True, exist_ok=True)
        (block_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def runtime_service_updates(self, marker: Path, log: Path, *, validation_rc: int = 0) -> dict:
        marker_text = repr(str(marker))
        log_text = repr(str(log))
        status_code = f"from pathlib import Path; import sys; sys.exit(0 if Path({marker_text}).exists() else 1)"
        stop_code = (
            f"from pathlib import Path; "
            f"Path({log_text}).open('a', encoding='utf-8').write('stop\\n'); "
            f"Path({marker_text}).unlink(missing_ok=True)"
        )
        start_code = (
            f"from pathlib import Path; "
            f"Path({log_text}).open('a', encoding='utf-8').write('start\\n'); "
            f"Path({marker_text}).write_text('running\\n', encoding='utf-8')"
        )
        validation_code = (
            f"from pathlib import Path; import sys; "
            f"running = Path({marker_text}).exists(); "
            f"Path({log_text}).open('a', encoding='utf-8').write('validate:' + ('running' if running else 'stopped') + '\\n'); "
            f"sys.exit({validation_rc} if not running else 17)"
        )
        return {
            "validation": {"commands": [{"name": "runtime-stopped-check", "argv": [sys.executable, "-c", validation_code]}]},
            "runtimeServices": {
                "test-service": {
                    "enabled": True,
                    "stopBeforePromotion": True,
                    "restartAfterCleanPromotion": True,
                    "statusCommand": [sys.executable, "-c", status_code],
                    "stopCommand": [sys.executable, "-c", stop_code],
                    "startCommand": [sys.executable, "-c", start_code],
                }
            },
        }

    def write_agent_queue_packet(self, repo: Path, *, candidate_id: str = "candidate:manual-agent-queue", shards: list[dict] | None = None, updates: dict | None = None) -> Path:
        config = load_closeout_config(repo)
        queue_root = repo / ".claude-state" / "closeout" / "agent-remediation"
        queue_dir = queue_root / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        pinned_refs: dict = {}
        exact_tuple = {
            "candidateId": candidate_id,
            "actionId": "resolve_conflicts_with_agent",
            "evidenceHash": "manual-evidence",
            "policyHash": config["policyHash"],
            "pinnedRefs": pinned_refs,
        }
        if shards is None:
            shards = [
                {
                    "shardId": "manual-01",
                    "candidateId": candidate_id,
                    "workBlockId": None,
                    "actionId": "resolve_conflicts_with_agent",
                    "evidenceHash": "manual-evidence",
                    "policyHash": config["policyHash"],
                    "pinnedRefs": pinned_refs,
                    "allowedReadScope": ["conflict.txt"],
                    "allowedWriteScope": ["conflict.txt"],
                    "resultPath": ".claude-state/closeout/agent-remediation/results/manual/manual-01.json",
                    "expectedOutputSchema": {"requiredFields": ["status", "changedPaths"]},
                    "validationRequirements": [],
                }
            ]
        packet = {
            "schemaVersion": "1.0",
            "status": "queued",
            "candidateId": candidate_id,
            "workBlockId": None,
            "actionId": "resolve_conflicts_with_agent",
            "actionClass": "agent_conflict_remediation",
            "evidenceHash": "manual-evidence",
            "policyHash": config["policyHash"],
            "pinnedRefs": pinned_refs,
            "exactTuple": exact_tuple,
            "requireExactTuple": True,
            "dirtyStateHash": agent_remediation_dirty_state_hash(repo),
            "shards": shards,
            "recoveryCommand": "recover manually",
        }
        if updates:
            packet = deep_update(packet, updates)
        path = queue_dir / f"{candidate_id.replace(':', '-')}.json"
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        return path

    def test_contract_parity_for_config_scripts_and_cli_surface(self) -> None:
        contract = broker_contract(ROOT)
        self.assertFalse(contract["missingConfigKeys"], contract)
        self.assertFalse(contract["missingScripts"], contract)
        self.assertIn("clean_integrate", contract["highImpactActions"])
        config = load_closeout_config(ROOT)
        self.assertEqual(config["git"]["targetBranch"], "master")
        self.assertEqual(config["git"]["remote"], "fork")
        self.assertTrue(config["workBlockBootstrap"]["autoBranchFromProtectedTarget"])
        self.assertEqual(config["workBlockBootstrap"]["branchPrefix"], "codex/work-block")
        self.assertTrue(config["workBlockBootstrap"]["requireIntegratedStartHeadForFinalize"])
        self.assertTrue(config["contentReviewGate"]["requireClaudeApprovalForFinalize"])
        self.assertEqual(config["contentReviewGate"]["coordinationFile"], ".claude-state/coordination/gpu-lane-impl-review-sync.md")
        self.assertEqual(config["contentReviewGate"]["handoffActor"], "CODEX")
        self.assertEqual(config["contentReviewGate"]["reviewActor"], "CLAUDE")
        self.assertEqual(
            config["contentReviewGate"]["authorizedReviewSessions"],
            ["5fc3fc6e-345f-40b8-bb3d-7abd6302b459"],
        )
        self.assertFalse(config["stashPolicy"]["allowForeignDirtyStash"])
        self.assertIn("pinnedRefs", config["reviewQuorum"]["tupleFields"])
        self.assertIn("repo_sweep_prune_merged", config["reviewQuorum"]["highImpactActions"])
        self.assertIn("split", config["reviewQuorum"]["highImpactActions"])
        self.assertIn("resolve_conflicts_with_agent", config["reviewQuorum"]["highImpactActions"])
        self.assertIn("checkpoint-owned-dirty", config["reviewQuorum"]["highImpactActions"])
        self.assertIn("dirtySplit", contract["requiredConfigKeys"])
        self.assertIn("workBlockBootstrap", contract["requiredConfigKeys"])
        self.assertIn("workBlockBootstrap.requireIntegratedStartHeadForFinalize", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate.requireClaudeApprovalForFinalize", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate.coordinationFile", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate.handoffActor", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate.reviewActor", contract["requiredConfigKeys"])
        self.assertIn("contentReviewGate.authorizedReviewSessions", contract["requiredConfigKeys"])
        self.assertIn("toolingBaseline", contract["requiredConfigKeys"])
        self.assertIn("powerShell", contract["requiredConfigKeys"])
        self.assertIn("powerShell.preferredExecutable", contract["requiredConfigKeys"])
        self.assertIn("powerShell.requiredArgs", contract["requiredConfigKeys"])
        self.assertIn("powerShell.windowsPowerShellOnly", contract["requiredConfigKeys"])
        self.assertIn("evidenceRepair", contract["requiredConfigKeys"])
        self.assertIn("responseHookLifecycle", contract["requiredConfigKeys"])
        self.assertIn("hardClean", contract["requiredConfigKeys"])
        self.assertIn("runtimeServices", contract["requiredConfigKeys"])
        self.assertIn("blockerAutoRemediation", contract["requiredConfigKeys"])
        self.assertIn("closeoutAddendumPersistence", contract["requiredConfigKeys"])
        self.assertIn("finalizeLoop", contract["requiredConfigKeys"])
        self.assertIn("agentRemediation", contract["requiredConfigKeys"])
        self.assertIn("agentRemediationQueue", contract["requiredConfigKeys"])
        self.assertIn("locking", contract["requiredConfigKeys"])
        self.assertIn("autoEligibilityRepair", contract["requiredConfigKeys"])
        self.assertIn("repoStateLedger", contract["requiredConfigKeys"])
        self.assertIn("webDashboardSpec", contract["requiredConfigKeys"])
        self.assertIn("webDashboardSpec.readOnlyByDefault", contract["requiredConfigKeys"])
        self.assertIn("webDashboardSpec.preserveClientStateAcrossRefresh", contract["requiredConfigKeys"])
        self.assertIn("webDashboardSpec.rollbackForbiddenActions", contract["requiredConfigKeys"])
        self.assertIn("rollbackPolicy", contract["requiredConfigKeys"])
        self.assertIn("dirty.cleanAtStartSensitiveAutoClaim", contract["requiredConfigKeys"])
        self.assertIn("dirty.cleanAtStartSensitiveAutoClaim", config["toolingBaseline"]["requiredConfigKeys"])
        self.assertIn("foreign_dirty_integrated_branch_prune", config["autoQuorum"]["autonomousActionClasses"])
        self.assertIn("detached_dirty_preserve", config["autoQuorum"]["autonomousActionClasses"])
        self.assertIn("owned_dirty_checkpoint", config["autoQuorum"]["autonomousActionClasses"])
        self.assertTrue(config["dirty"]["autoClaimCleanAtStart"])
        self.assertIn(".claude/analysis/*.md", config["dirty"]["cleanAtStartSensitiveAutoClaim"])
        self.assertEqual(config["powerShell"]["preferredExecutable"], "pwsh.exe")
        self.assertEqual(config["powerShell"]["fallbackExecutable"], "powershell.exe")
        self.assertIn("-NoProfile", config["powerShell"]["requiredArgs"])
        self.assertIn("-NonInteractive", config["powerShell"]["requiredArgs"])
        self.assertIn("agent bridge process metadata probes", config["powerShell"]["preferFor"])
        self.assertIn("PowerShell 7+", config["powerShell"]["justification"])
        bridge_validation = next(command for command in config["validation"]["commands"] if command["name"] == "agent-bridge-wmi-regression")
        self.assertIn(
            "tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_bootstrap_watcher_enumeration_prefers_pwsh_for_cim",
            bridge_validation["argv"],
        )
        self.assertIn(
            "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_queries_fall_back_to_windows_powershell",
            bridge_validation["argv"],
        )
        self.assertIn(
            "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_trampoline_host_env_queries_prefer_pwsh_for_cim",
            bridge_validation["argv"],
        )
        self.assertIn(
            "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_server_wrapper_process_entry_prefers_pwsh_for_cim",
            bridge_validation["argv"],
        )
        self.assertIn(".codex-state/**", config["paths"]["generated"])
        self.assertIn("test_existing_analysis_note_clean_at_start_is_auto_claimed_despite_sensitive_root", config["toolingBaseline"]["requiredTests"])
        self.assertIn("test_new_analysis_note_under_sensitive_root_still_blocks_as_unowned", config["toolingBaseline"]["requiredTests"])
        self.assertTrue(config["repoSweep"]["retainedBlockerAutoRemediation"]["enabled"])
        self.assertEqual(config["repoSweep"]["recoveryBranchPrefix"], "closeout/recovery/detached")
        self.assertTrue(config["repoSweep"]["allowForeignDirtyIntegratedBranchPrune"])
        self.assertTrue(config["repoSweep"]["allowPatchEquivalentPrune"])
        self.assertEqual(config["repoSweep"]["protectedLockedWorktreeExactPolicy"], [])
        self.assertFalse(config["repoSweep"]["auditedBulkOverride"]["enabled"])
        self.assertTrue(config["repoSweep"]["evidencePreservingPrune"]["enabled"])
        self.assertEqual(config["repoSweep"]["evidencePreservingPrune"]["recoveryRoot"], ".claude-state/closeout/manual-prune")
        self.assertTrue(config["repoSweep"]["evidencePreservingPrune"]["requireBundleForNonAncestor"])
        self.assertTrue(config["repoSweep"]["evidencePreservingPrune"]["requireDirtyWorktreeBytePreservation"])
        self.assertTrue(config["repoSweep"]["evidencePreservingPrune"]["requireReviewerVerdicts"])
        self.assertTrue(config["repoSweep"]["evidencePreservingPrune"]["rerunSweepAfterPrune"])
        self.assertTrue(config["closeoutAddendumPersistence"]["enabled"])
        self.assertTrue(config["closeoutAddendumPersistence"]["sameTurnRequired"])
        self.assertTrue(config["finalizeLoop"]["enabled"])
        self.assertEqual(config["finalizeLoop"]["maxRetries"], 8)
        self.assertEqual(config["finalizeLoop"]["safeSecondOrderRepairs"]["final_push_evidence_repaired"], "evidence_repair")
        self.assertEqual(config["finalizeLoop"]["safeSecondOrderRepairs"]["target_push_rerun_required"], "target_push_recovery")
        self.assertEqual(config["finalizeLoop"]["safeSecondOrderRepairs"]["stale_review"], "renew_stale_review")
        self.assertEqual(config["finalizeLoop"]["safeSecondOrderRepairs"]["validation_failed"], "rerun_validation_smoke")
        self.assertEqual(config["evidenceRepair"]["commitMessage"], "chore(closeout): repair closeout evidence artifacts")
        self.assertTrue(config["agentRemediation"]["enabled"])
        self.assertIn("codex-desktop", config["agentRemediation"]["surfaceAdapters"])
        self.assertTrue(config["agentRemediationQueue"]["enabled"])
        self.assertIn(".claude-state/closeout/agent-remediation", config["agentRemediationQueue"]["queueRoots"])
        self.assertEqual(config["agentRemediationQueue"]["surfaceUnavailableStatus"], "agent_remediation_surface_unavailable")
        self.assertIn("agent_conflict_remediation", config["autoQuorum"]["autonomousActionClasses"])
        self.assertEqual(config["responseHookLifecycle"]["skipSessionWorktreeSignal"], "SkipSessionWorktree")
        self.assertEqual(config["responseHookLifecycle"]["readOnlyHookPhases"], ["response", "final"])
        self.assertEqual(config["responseHookLifecycle"]["managedSessionWorktreeRoots"], [".codex-worktrees/**"])
        self.assertTrue(config["responseHookLifecycle"]["bootstrapAllowedOnlyByExplicitStart"])
        self.assertTrue(config["hardClean"]["requireForCompletion"])
        self.assertTrue(config["hardClean"]["allowRetainedForeignDirtyAtCompletion"])
        self.assertTrue(config["hardClean"]["requireNoStash"])
        self.assertTrue(config["hardClean"]["protectedTargetNoopCloseout"]["enabled"])
        self.assertTrue(config["hardClean"]["unifiedTruthReport"]["enabled"])
        self.assertEqual(config["hardClean"]["unifiedTruthReport"]["authoritativeSource"], "repoClosedPostcondition.closeoutCleanTruth")
        self.assertIn(".claude-state/**", config["hardClean"]["exemptStatusPatterns"])
        self.assertIn(".codex-state/**", config["hardClean"]["exemptStatusPatterns"])
        self.assertFalse(config["runtimeServices"]["mlvapp-preview"]["enabled"])
        self.assertGreater(config["locking"]["detectTimeoutMs"], 0)
        self.assertGreater(config["locking"]["finalizeTimeoutMs"], 0)
        self.assertGreater(config["locking"]["maxProcessOutputBytes"], 0)
        self.assertIn("review_quorum_missing", config["locking"]["failureTextPatterns"])
        self.assertGreater(config["autoEligibilityRepair"]["timeoutMs"], 0)
        self.assertTrue(config["repoStateLedger"]["enabled"])
        self.assertEqual(config["repoStateLedger"]["artifactSchema"], "repo-state-snapshot.v1")
        self.assertEqual(config["repoStateLedger"]["latestPath"], ".claude-state/closeout/repo-state/latest.json")
        self.assertEqual(config["repoStateLedger"]["auditType"], "repo_state_snapshot")
        self.assertIn("closeoutHistory", config["repoStateLedger"]["include"])
        self.assertGreater(config["repoStateLedger"]["closeoutHistoryLimit"], 0)
        self.assertEqual(config["repoStateLedger"]["closeoutHistorySchema"], "closeout-history-index.v1")
        self.assertFalse(config["repoStateLedger"]["liveRefreshWritesHistory"])
        self.assertIn("write-repo-state.ps1", config["repoStateLedger"]["liveRefreshCommand"])
        self.assertFalse(config["repoStateLedger"]["feedPathPolicy"]["latestJsonIsRollbackEvidence"])
        self.assertTrue(config["webDashboardSpec"]["enabled"])
        self.assertEqual(config["webDashboardSpec"]["localUrl"], "http://127.0.0.1:8765/closeout")
        self.assertEqual(config["webDashboardSpec"]["stickyUrlPath"], "/closeout")
        self.assertEqual(config["webDashboardSpec"]["refreshTransport"], "sse-with-polling-fallback")
        self.assertGreater(config["webDashboardSpec"]["autoRefreshMs"], 0)
        self.assertIn("write-repo-state.ps1", config["webDashboardSpec"]["refreshCommand"])
        self.assertTrue(config["webDashboardSpec"]["preserveClientStateAcrossRefresh"])
        self.assertIn("selectedWorkBlockId", config["webDashboardSpec"]["preservedClientStateKeys"])
        self.assertEqual(config["webDashboardSpec"]["feedAuthority"], "latest-json-is-display-feed-only")
        self.assertEqual(config["webDashboardSpec"]["mutationModel"], "symbolic-action-request-only")
        self.assertEqual(config["webDashboardSpec"]["duplicateLaunchPolicy"], "reuse-same-repo-fail-foreign-owner")
        self.assertEqual(config["webDashboardSpec"]["helper"]["scriptPath"], "tools\\closeout\\start-closeout-dashboard.ps1")
        self.assertEqual(config["webDashboardSpec"]["helper"]["serverProcessIdSource"], "/api/closeout/actions")
        self.assertEqual(config["webDashboardSpec"]["endpoints"]["page"], "/closeout")
        self.assertEqual(config["webDashboardSpec"]["endpoints"]["actions"], "/api/closeout/actions")
        self.assertEqual(config["webDashboardSpec"]["endpoints"]["actionsRequestHistory"], "/api/closeout/actions/requests")
        self.assertIn("historical-closeouts", config["webDashboardSpec"]["views"])
        self.assertIn("workflow-comparison", config["webDashboardSpec"]["views"])
        self.assertIn("repo-map", config["webDashboardSpec"]["primaryPanels"])
        self.assertIn("workflow-comparison", config["webDashboardSpec"]["primaryPanels"])
        self.assertIn("action-request-history", config["webDashboardSpec"]["primaryPanels"])
        self.assertTrue(config["rollbackPolicy"]["enabled"])
        self.assertIn("git-revert", config["rollbackPolicy"]["allowedStrategies"])
        self.assertTrue(config["rollbackPolicy"]["startNewWorkBlockForRollback"])
        self.assertTrue(config["rollbackPolicy"]["requireUserApprovalForRollback"])
        self.assertTrue(config["rollbackPolicy"]["neverUseResetHardWithoutExplicitUserRequest"])
        self.assertTrue(config["rollbackPolicy"]["requireRecoveryCommandInMutatingAudits"])
        self.assertTrue(config["rollbackPolicy"]["writeRollbackPlanBeforeMutation"])
        self.assertEqual(config["rollbackPolicy"]["readinessSchema"], "rollback-readiness.v1")
        self.assertEqual(config["rollbackPolicy"]["requiredManifestSchema"], "closeout-rollback-manifest.v1")
        self.assertEqual(config["rollbackPolicy"]["validationSchema"], "closeout-rollback-manifest-validation.v1")
        self.assertEqual(config["rollbackPolicy"]["manifestRoot"], ".claude-state/closeout/rollback")
        self.assertIn("validate-rollback-manifest.ps1", config["rollbackPolicy"]["validatorCommand"])
        self.assertEqual(config["rollbackPolicy"]["validatorActionability"], "read-only-validator")
        self.assertEqual(config["rollbackPolicy"]["readinessDefaultActionability"], "read-only-no-actor")
        self.assertFalse(config["rollbackPolicy"]["latestFeedIsRollbackEvidence"])
        self.assertTrue(config["rollbackPolicy"]["requireImmutableSourceSnapshotForRollback"])
        self.assertIn("sourceSnapshotPath", config["rollbackPolicy"]["requiredManifestFields"])
        self.assertIn("sourceSnapshotAuditHash", config["rollbackPolicy"]["requiredManifestFields"])
        self.assertIn("repoClosedAuditHash", config["rollbackPolicy"]["requiredManifestFields"])
        self.assertIn("reset-hard", config["rollbackPolicy"]["disallowedDefaultActions"])
        self.assertIn("delete-evidence", config["rollbackPolicy"]["disallowedDefaultActions"])

    def test_content_review_authorized_session_rotation_is_strict_singleton(self) -> None:
        current = "5fc3fc6e-345f-40b8-bb3d-7abd6302b459"
        retired = "ee961390-55ba-48de-" + "8220-095eda6ad492"
        override = json.loads((ROOT / "closeout.config.json").read_text(encoding="utf-8"))
        effective = load_closeout_config(ROOT)

        self.assertEqual(override["contentReviewGate"]["authorizedReviewSessions"], [current])
        self.assertEqual(DEFAULT_CLOSEOUT_CONFIG["contentReviewGate"]["authorizedReviewSessions"], [current])
        self.assertEqual(effective["contentReviewGate"]["authorizedReviewSessions"], [current])
        for path in (
            ROOT / "closeout.config.json",
            ROOT / "tools" / "repo_hygiene" / "brokered_closeout.py",
            Path(__file__),
        ):
            self.assertNotIn(retired, path.read_text(encoding="utf-8"), str(path))

        start = "c" * 40
        feature = "f" * 40
        range_token = "%s..%s" % (start, feature)
        effective["contentReviewGate"]["coordinationFile"] = "gate.md"
        detection = {"featureHead": feature, "targetHead": start, "targetBranch": "master"}
        manifest = {"startHead": start}

        def validate_seat(root: Path, seat: str):
            (root / "gate.md").write_text(
                "### [2026-01-01T00:00:00Z] CODEX - HANDOFF (ready)\n"
                "Range: `%s`\n" % range_token
                + "### [2026-01-01T01:00:00Z] CLAUDE - REVIEW (verdict)\n"
                + "Range: `%s`\n" % range_token
                + "Verdict: APPROVE\n"
                + "Seat: %s\n" % seat,
                encoding="utf-8",
            )
            return validate_content_review_approval_for_finalize(root, effective, detection, manifest)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(validate_seat(root, current))
            denied = validate_seat(root, retired)
            self.assertIsNotNone(denied)
            self.assertEqual(denied["reason"], "content_approval_unauthorized_seat")

    @unittest.skipUnless(os.name == "nt", "PowerShell path policy is Windows-specific")
    def test_powershell_policy_prefers_pwsh_no_profile_for_closeout_commands(self) -> None:
        config = load_closeout_config(ROOT)
        policy = powershell_policy(config)
        self.assertEqual(policy["preferredExecutable"], "pwsh.exe")
        self.assertEqual(policy["fallbackExecutable"], "powershell.exe")
        self.assertTrue(policy["fallbackOnlyWhenPwshUnavailable"])
        self.assertIn("-NoProfile", policy["requiredArgs"])
        self.assertIn("-NonInteractive", policy["requiredArgs"])
        self.assertIn("PowerShell 7+", policy["justification"])
        self.assertIn("tools/agent-bridge/codex_bridge_reminder.ps1 WinRT toast activation", policy["windowsPowerShellOnly"])
        self.assertIn("tools/agent-bridge/wake_codex.ps1 Windows shell activation", policy["windowsPowerShellOnly"])
        self.assertIn("tools/agent-bridge/watcher.py WinRT toast notification activation", policy["windowsPowerShellOnly"])
        self.assertIn("tools/agent-bridge/watcher.py WinForms balloon fallback", policy["windowsPowerShellOnly"])

        expected_prefix = "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass"

        def pwsh_available(name):
            return "C:/Program Files/PowerShell/7/pwsh.exe" if name == "pwsh.exe" else None

        with mock.patch("tools.repo_hygiene.brokered_closeout.os.name", "nt"), mock.patch("tools.repo_hygiene.brokered_closeout.shutil.which", side_effect=pwsh_available):
            generated = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
            self.assertEqual(generated, expected_prefix + " -File tools\\closeout\\repo-sweep-closeout.ps1 -RepoRoot . -Apply")
            self.assertEqual(
                effective_closeout_script_command(config, "write-repo-state.ps1", ["-Write", "-LatestOnly"], config["repoStateLedger"]["liveRefreshCommand"]),
                expected_prefix + " -File tools\\closeout\\write-repo-state.ps1 -RepoRoot . -Write -LatestOnly",
            )
            self.assertEqual(
                effective_closeout_script_command(config, "write-repo-state.ps1", ["-Write", "-LatestOnly"], config["webDashboardSpec"]["refreshCommand"]),
                expected_prefix + " -File tools\\closeout\\write-repo-state.ps1 -RepoRoot . -Write -LatestOnly",
            )

        with mock.patch("tools.repo_hygiene.brokered_closeout.os.name", "nt"), mock.patch("tools.repo_hygiene.brokered_closeout.shutil.which", return_value=None):
            self.assertEqual(powershell_executable_for_policy(policy), "powershell.exe")
            fallback_command = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
        self.assertTrue(fallback_command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive"), fallback_command)
        custom_command = "custom-refresh --repo ."
        with self.assertRaisesRegex(HygieneError, "unsupported configured closeout command"):
            effective_closeout_script_command(config, "write-repo-state.ps1", ["-Write", "-LatestOnly"], custom_command)

        repo = self.init_repo(remote=True)
        with mock.patch("tools.repo_hygiene.brokered_closeout.os.name", "nt"), mock.patch("tools.repo_hygiene.brokered_closeout.shutil.which", side_effect=pwsh_available):
            snapshot = repo_state_snapshot(repo, write=False)
            actions = dashboard_actions_payload(repo)
        dashboard_commands = [
            snapshot["dashboard"]["refreshCommand"],
            snapshot["stateLedger"]["liveRefreshCommand"],
            actions["symbolicActions"][0]["command"],
            actions["symbolicActions"][2]["command"],
        ]
        for command in dashboard_commands:
            self.assertTrue(command.startswith(expected_prefix), command)
            self.assertNotIn("powershell -NoProfile", command)
        with mock.patch("tools.repo_hygiene.brokered_closeout.os.name", "nt"), mock.patch("tools.repo_hygiene.brokered_closeout.shutil.which", return_value=None):
            fallback_snapshot = repo_state_snapshot(repo, write=False)
            fallback_actions = dashboard_actions_payload(repo)
        self.assertTrue(fallback_snapshot["dashboard"]["refreshCommand"].startswith("powershell.exe -NoLogo -NoProfile -NonInteractive"))
        self.assertTrue(fallback_snapshot["stateLedger"]["liveRefreshCommand"].startswith("powershell.exe -NoLogo -NoProfile -NonInteractive"))
        self.assertTrue(fallback_actions["symbolicActions"][0]["command"].startswith("powershell.exe -NoLogo -NoProfile -NonInteractive"))

    def test_repo_state_snapshot_writes_dashboard_ready_ledger_and_audit(self) -> None:
        repo = self.init_repo(remote=True)
        (repo / "dashboard-dirty.txt").write_text("dashboard dirty\n", encoding="utf-8")
        compare_result_path = repo / ".claude-state" / "closeout" / "workflow-comparison" / "compare-result.json"
        compare_result_path.parent.mkdir(parents=True, exist_ok=True)
        compare_result_path.write_text(
            json.dumps(
                {
                    "artifactType": "closeout-compare-result.v1",
                    "schemaVersion": 1,
                    "schema": "closeout-compare-result.v1",
                    "status": "current",
                    "generatedAt": "2026-05-14T21:20:00Z",
                    "freshnessMarkerOrTimestamp": "Last updated: 2026-05-14 16:20 -05:00",
                    "snapshotPointer": {
                        "schema": "repo-state-snapshot.v1",
                        "path": ".claude-state/closeout/repo-state/latest.json",
                        "hash": "example-hash",
                        "workBlockId": "wb-dashboard",
                    },
                    "reportEnvelope": {
                        "objective": "Report the current closeout workflow in a mechanically comparable shape.",
                        "lastCompletedWork": "Seeded the live compare-result pointer for the dashboard snapshot test.",
                        "nextSteps": ["Keep the live pointer in sync with the compare-result artifact."],
                        "blockers": [],
                        "freshnessMarkerOrTimestamp": "Last updated: 2026-05-14 16:20 -05:00",
                        "compareFindings": [],
                    },
                    "compareFindings": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        write_audit(
            repo,
            load_closeout_config(repo),
            "repo_closed_postcondition",
            {
                "ok": True,
                "status": "success",
                "closeoutCleanTruth": {
                    "artifactKind": "closeoutCleanTruth",
                    "status": "clean",
                    "authority": "repoClosedPostcondition",
                },
            },
            work_block_id="wb-dashboard",
            outcome="success",
        )

        snapshot = repo_state_snapshot(repo, write=True, work_block_id="wb-dashboard")

        self.assertEqual(snapshot["artifactSchema"], "repo-state-snapshot.v1")
        self.assertEqual(snapshot["artifactKind"], "repoStateSnapshot")
        self.assertEqual(snapshot["branch"]["currentBranch"], "master")
        self.assertEqual(snapshot["branch"]["tracking"]["upstream"], "origin/master")
        self.assertEqual(snapshot["dirty"]["entryCount"], 1)
        self.assertEqual(snapshot["dirty"]["entries"][0]["path"], "dashboard-dirty.txt")
        self.assertEqual(snapshot["closeout"]["history"]["schema"], "closeout-history-index.v1")
        self.assertEqual(snapshot["closeout"]["history"]["workBlockCount"], 1)
        self.assertEqual(snapshot["closeout"]["history"]["entryCount"], 1)
        self.assertEqual(snapshot["closeout"]["history"]["entries"][0]["workBlockId"], "wb-dashboard")
        self.assertEqual(snapshot["closeout"]["history"]["recentWorkBlocks"][0]["workBlockId"], "wb-dashboard")
        self.assertTrue(snapshot["closeout"]["history"]["recentWorkBlocks"][0]["repoClosedOk"])
        self.assertEqual(snapshot["closeout"]["history"]["recentWorkBlocks"][0]["latestCloseoutCleanTruth"]["status"], "clean")
        self.assertEqual(snapshot["dashboard"]["localUrl"], "http://127.0.0.1:8765/closeout")
        self.assertEqual(snapshot["dashboard"]["stickyUrlPath"], "/closeout")
        self.assertEqual(snapshot["dashboard"]["refreshTransport"], "sse-with-polling-fallback")
        self.assertGreater(snapshot["dashboard"]["autoRefreshMs"], 0)
        self.assertIn("write-repo-state.ps1", snapshot["dashboard"]["refreshCommand"])
        self.assertEqual(snapshot["dashboard"]["refreshCommandPolicy"], "repo-owned-write-repo-state-latest-only")
        self.assertFalse(snapshot["dashboard"]["liveRefreshWritesHistory"])
        self.assertEqual(snapshot["dashboard"]["mutationModel"], "symbolic-action-request-only")
        self.assertIn("selectedWorkBlockId", snapshot["dashboard"]["preservedClientStateKeys"])
        self.assertEqual(snapshot["dashboard"]["feedAuthority"], "latest-json-is-display-feed-only")
        self.assertEqual(snapshot["dashboard"]["duplicateLaunchPolicy"], "reuse-same-repo-fail-foreign-owner")
        self.assertEqual(snapshot["dashboard"]["helper"]["scriptPath"], "tools\\closeout\\start-closeout-dashboard.ps1")
        self.assertEqual(snapshot["dashboard"]["endpoints"]["latest"], "/api/closeout/repo-state/latest")
        self.assertEqual(snapshot["dashboard"]["endpoints"]["actionsPreview"], "/api/closeout/actions/preview")
        self.assertEqual(snapshot["dashboard"]["endpoints"]["actionsRequestHistory"], "/api/closeout/actions/requests")
        self.assertEqual(snapshot["dashboard"]["workflowComparison"]["compareResultPath"], ".claude-state/closeout/workflow-comparison/compare-result.json")
        self.assertTrue(snapshot["dashboard"]["workflowComparison"]["compareResultAvailable"])
        self.assertEqual(snapshot["dashboard"]["workflowComparison"]["compareResult"]["artifactType"], "closeout-compare-result.v1")
        self.assertEqual(snapshot["dashboard"]["workflowComparison"]["compareResult"]["schemaVersion"], 1)
        self.assertEqual(snapshot["dashboard"]["workflowComparison"]["compareResult"]["status"], "current")
        self.assertEqual(snapshot["dashboard"]["workflowComparison"]["compareResult"]["snapshotPointer"]["workBlockId"], "wb-dashboard")
        self.assertEqual(snapshot["dashboard"]["helper"]["serverProcessIdSource"], snapshot["dashboard"]["endpoints"]["actions"])
        self.assertEqual(snapshot["dashboard"]["helper"]["readinessEndpoint"], snapshot["dashboard"]["endpoints"]["actions"])
        self.assertIn("audit-timeline", snapshot["dashboard"]["primaryPanels"])
        self.assertIn("workflow-comparison", snapshot["dashboard"]["primaryPanels"])
        self.assertIn("action-preview", snapshot["dashboard"]["primaryPanels"])
        self.assertIn("action-request-history", snapshot["dashboard"]["primaryPanels"])
        self.assertEqual(snapshot["worktreeInspection"]["schema"], "worktree-inspection.v1")
        self.assertTrue(snapshot["worktreeInspection"]["currentRootPresent"])
        self.assertEqual(snapshot["worktreeInspection"]["ordinaryLinkedWorktreeCount"], 0)
        self.assertEqual(snapshot["worktreeInspection"]["inspectionFailures"], [])
        self.assertIn("path-restore-from-snapshot", snapshot["rollback"]["allowedStrategies"])
        self.assertTrue(snapshot["rollback"]["requireUserApprovalForRollback"])
        self.assertTrue(snapshot["rollback"]["neverUseResetHardWithoutExplicitUserRequest"])
        self.assertIn(".claude-state/closeout/manual-prune", snapshot["rollback"]["recoveryEvidenceRoots"])
        self.assertEqual(snapshot["rollback"]["manifestRoot"], ".claude-state/closeout/rollback")
        self.assertEqual(snapshot["rollback"]["validationSchema"], "closeout-rollback-manifest-validation.v1")
        self.assertIn("validate-rollback-manifest.ps1", snapshot["rollback"]["validatorCommand"])
        self.assertEqual(snapshot["rollback"]["validatorActionability"], "read-only-validator")
        self.assertEqual(snapshot["rollback"]["readiness"]["schema"], "rollback-readiness.v1")
        self.assertEqual(snapshot["rollback"]["readiness"]["actionability"], "read-only-no-actor")
        self.assertFalse(snapshot["rollback"]["readiness"]["evidenceFresh"])
        self.assertIn("rollback actor has not validated", snapshot["rollback"]["readiness"]["readinessReason"])
        self.assertFalse(snapshot["rollback"]["readiness"]["latestFeedIsRollbackEvidence"])
        self.assertEqual(snapshot["rollback"]["readiness"]["requiredManifestSchema"], "closeout-rollback-manifest.v1")
        self.assertTrue((repo / snapshot["stateLedger"]["latestPath"]).exists())
        self.assertTrue((repo / snapshot["stateLedger"]["historyPath"]).exists())
        self.assertEqual(snapshot["stateLedger"]["writeMode"], "latest-and-history")
        self.assertEqual(snapshot["stateLedger"]["closeoutHistorySchema"], "closeout-history-index.v1")
        self.assertFalse(snapshot["stateLedger"]["liveRefreshWritesHistory"])
        self.assertEqual(snapshot["stateLedger"]["refreshCommandPolicy"], "repo-owned-write-repo-state-latest-only")
        self.assertFalse(snapshot["stateLedger"]["feedPathPolicy"]["latestJsonIsRollbackEvidence"])
        self.assertTrue(snapshot["stateLedger"]["feedPathPolicy"]["historyJsonIsImmutableRollbackEvidence"])
        self.assertEqual(snapshot["stateLedger"]["feedPathPolicy"]["latestFeedUse"], "display-only-dashboard-feed")
        self.assertEqual(snapshot["stateLedger"]["feedPathPolicy"]["historyEvidenceUse"], "immutable-history-source-for-rollback-actor")
        self.assertIn("repo_state_snapshot", self.audit_types(repo))
        audit = json.loads((repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(audit["payload"]["latestPath"], ".claude-state/closeout/repo-state/latest.json")
        self.assertEqual(audit["payload"]["dirtyEntryCount"], 1)
        self.assertEqual(audit["payload"]["linkedSiblingWorktreeCount"], 0)
        self.assertEqual(audit["payload"]["worktreeInspectionFailureCount"], 0)
        self.assertEqual(audit["payload"]["writeMode"], "latest-and-history")

        custom_repo = self.init_repo(
            remote=True,
            config_updates={"webDashboardSpec": {"endpoints": {"actions": "/custom/closeout/actions"}}},
        )
        custom_snapshot = repo_state_snapshot(custom_repo, write=False)
        self.assertEqual(custom_snapshot["dashboard"]["endpoints"]["actions"], "/custom/closeout/actions")
        self.assertEqual(custom_snapshot["dashboard"]["helper"]["serverProcessIdSource"], "/custom/closeout/actions")
        self.assertEqual(custom_snapshot["dashboard"]["helper"]["readinessEndpoint"], "/custom/closeout/actions")
        custom_actions = dashboard_actions_payload(custom_repo, server_process_id=1234)
        self.assertEqual(custom_actions["endpoints"]["actions"], "/custom/closeout/actions")
        self.assertEqual(custom_actions["helper"]["serverProcessIdSource"], "/custom/closeout/actions")
        self.assertEqual(custom_actions["helper"]["readinessEndpoint"], "/custom/closeout/actions")

        explicit_helper_repo = self.init_repo(
            remote=True,
            config_updates={
                "webDashboardSpec": {
                    "endpoints": {"actions": "/custom/closeout/actions"},
                    "helper": {
                        "serverProcessIdSource": "/explicit/helper/actions",
                        "readinessEndpoint": "/explicit/helper/ready",
                    },
                }
            },
        )
        explicit_snapshot = repo_state_snapshot(explicit_helper_repo, write=False)
        self.assertEqual(explicit_snapshot["dashboard"]["helper"]["serverProcessIdSource"], "/explicit/helper/actions")
        self.assertEqual(explicit_snapshot["dashboard"]["helper"]["readinessEndpoint"], "/explicit/helper/ready")
        explicit_actions = dashboard_actions_payload(explicit_helper_repo, server_process_id=1234)
        self.assertEqual(explicit_actions["helper"]["serverProcessIdSource"], "/explicit/helper/actions")
        self.assertEqual(explicit_actions["helper"]["readinessEndpoint"], "/explicit/helper/ready")

    def test_repo_state_snapshot_reports_worktree_inspection(self) -> None:
        repo = self.init_repo(remote=True)
        sibling = self.tempdir / "sibling-worktree"
        git(repo, "branch", "codex/sibling-worktree")
        git(repo, "worktree", "add", str(sibling), "codex/sibling-worktree")

        snapshot = repo_state_snapshot(repo, write=False)

        inspection = snapshot["worktreeInspection"]
        self.assertEqual(inspection["schema"], "worktree-inspection.v1")
        self.assertEqual(inspection["status"], "success")
        self.assertTrue(inspection["currentRootPresent"])
        self.assertEqual(inspection["linkedWorktreeCount"], 1)
        self.assertEqual(inspection["ordinaryLinkedWorktreeCount"], 1)
        self.assertEqual(Path(inspection["ordinaryLinkedWorktrees"][0]["path"]).resolve(), sibling.resolve())
        self.assertEqual(snapshot["worktrees"], inspection["worktrees"])

    def test_repo_state_latest_only_refresh_updates_feed_without_audit_noise(self) -> None:
        repo = self.init_repo(remote=True)
        first = repo_state_snapshot(repo, write=True, work_block_id="wb-dashboard")
        audit_path = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        history_root = repo / first["stateLedger"]["historyRoot"]
        latest_path = repo / first["stateLedger"]["latestPath"]
        audit_before = audit_path.read_text(encoding="utf-8").splitlines()
        history_before = sorted(history_root.glob("*.json"))
        history_bytes_before = {path.name: path.read_bytes() for path in history_before}

        (repo / "live-refresh.txt").write_text("dashboard refresh dirty\n", encoding="utf-8")
        latest = repo_state_snapshot(repo, write=True, latest_only=True, work_block_id="wb-dashboard")

        audit_after = audit_path.read_text(encoding="utf-8").splitlines()
        history_after = sorted(history_root.glob("*.json"))
        persisted = json.loads(latest_path.read_text(encoding="utf-8"))

        self.assertEqual(latest["stateLedger"]["writeMode"], "latest-only")
        self.assertNotIn("historyPath", latest["stateLedger"])
        self.assertEqual(audit_after, audit_before)
        self.assertEqual(history_after, history_before)
        self.assertEqual({path.name: path.read_bytes() for path in history_after}, history_bytes_before)
        self.assertTrue(persisted["dirty"]["entryCount"] >= 1)
        self.assertIn("live-refresh.txt", {entry["path"] for entry in persisted["dirty"]["entries"]})
        self.assertEqual(persisted["stateLedger"]["writeMode"], "latest-only")
        self.assertIn("write-repo-state.ps1", persisted["stateLedger"]["liveRefreshCommand"])
        self.assertEqual(persisted["stateLedger"]["refreshCommandPolicy"], "repo-owned-write-repo-state-latest-only")
        self.assertTrue(persisted["stateLedger"]["feedPathPolicy"]["historyJsonIsImmutableRollbackEvidence"])
        self.assertFalse(persisted["rollback"]["readiness"]["evidenceFresh"])
        self.assertFalse(persisted["rollback"]["readiness"]["latestFeedIsRollbackEvidence"])

    def test_repo_state_rollback_readiness_fails_closed_without_actor(self) -> None:
        repo = self.init_repo(remote=True)

        snapshot = repo_state_snapshot(repo, write=True, work_block_id="wb-dashboard")
        readiness = snapshot["rollback"]["readiness"]
        latest_path = repo / snapshot["stateLedger"]["latestPath"]
        persisted = json.loads(latest_path.read_text(encoding="utf-8"))

        self.assertEqual(readiness["schema"], "rollback-readiness.v1")
        self.assertEqual(readiness["actionability"], "read-only-no-actor")
        self.assertEqual(readiness["evidenceStatus"], "not_evaluated_by_rollback_actor")
        self.assertFalse(readiness["evidenceFresh"])
        self.assertIsNone(readiness["sourceSnapshotPath"])
        self.assertIsNone(readiness["sourceSnapshotHash"])
        self.assertFalse(readiness["latestFeedIsRollbackEvidence"])
        self.assertTrue(readiness["requiresImmutableSourceSnapshot"])
        self.assertEqual(readiness["requiredManifestSchema"], "closeout-rollback-manifest.v1")
        self.assertIn("sourceSnapshotPath", readiness["requiredManifestFields"])
        self.assertIn("sourceSnapshotAuditHash", readiness["requiredManifestFields"])
        self.assertIn("repoClosedAuditHash", readiness["requiredManifestFields"])
        self.assertNotEqual(persisted["stateLedger"]["latestPath"], readiness["sourceSnapshotPath"])

    def test_validate_rollback_manifest_accepts_immutable_history_and_audit(self) -> None:
        repo = self.init_repo(remote=True)
        config = load_closeout_config(repo)
        self.write_closeout_work_block_manifest(repo, "wb-rollback")
        verify_repo_closed_postcondition(repo, config, work_block_id="wb-rollback", finalize_result={"status": "success"})
        snapshot = repo_state_snapshot(repo, write=True, work_block_id="wb-rollback")
        repo_closed_audit = next(row for row in self.audit_rows(repo) if row["auditType"] == "repo_closed_postcondition")
        source_audit = next(
            row
            for row in self.audit_rows(repo)
            if row["auditType"] == "repo_state_snapshot" and row.get("payload", {}).get("snapshotHash") == snapshot["stateLedger"]["snapshotHash"]
        )
        manifest_dir = repo / ".claude-state" / "closeout" / "rollback"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": "closeout-rollback-manifest.v1",
            "targetHead": snapshot["branch"]["head"],
            "sourceSnapshotPath": snapshot["stateLedger"]["historyPath"],
            "sourceSnapshotHash": snapshot["stateLedger"]["snapshotHash"],
            "sourceSnapshotAuditHash": source_audit["auditHash"],
            "repoClosedAuditHash": repo_closed_audit["auditHash"],
            "policyHash": config["policyHash"],
            "plannedStrategy": "git-revert",
            "userApproval": True,
            "recoveryCommand": "git revert --no-edit " + snapshot["branch"]["head"],
        }
        manifest_path = manifest_dir / "valid.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        result = validate_rollback_manifest(repo, ".claude-state/closeout/rollback/valid.json")

        self.assertEqual(result["schema"], "closeout-rollback-manifest-validation.v1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["actionability"], "read-only-validator")
        self.assertFalse(result["actionActorAvailable"])
        self.assertFalse(result["mutationReady"])
        self.assertFalse(result["latestFeedIsRollbackEvidence"])
        self.assertEqual(result["sourceSnapshotHash"], snapshot["stateLedger"]["snapshotHash"])
        self.assertEqual(result["sourceSnapshotHashScope"], "repo-state-snapshot without stateLedger.snapshotHash/historyPath")
        self.assertEqual(result["repoClosedAuditHash"], repo_closed_audit["auditHash"])
        self.assertEqual(result["sourceSnapshotAuditHash"], source_audit["auditHash"])
        self.assertEqual(result["workBlockId"], "wb-rollback")
        self.assertTrue(result["sourceSnapshotAuditSidecarPath"])
        self.assertTrue(result["repoClosedAuditSidecarPath"])

        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.repo_hygiene.work_block_cli",
                "--repo-root",
                str(repo),
                "validate-rollback-manifest",
                "--manifest-path",
                ".claude-state/closeout/rollback/valid.json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["status"], "success")

    def test_validate_rollback_manifest_rejects_latest_forbidden_and_stale_evidence(self) -> None:
        repo = self.init_repo(remote=True)
        config = load_closeout_config(repo)
        self.write_closeout_work_block_manifest(repo, "wb-rollback")
        verify_repo_closed_postcondition(repo, config, work_block_id="wb-rollback", finalize_result={"status": "success"})
        snapshot = repo_state_snapshot(repo, write=True, work_block_id="wb-rollback")
        repo_closed_audit = next(row for row in self.audit_rows(repo) if row["auditType"] == "repo_closed_postcondition")
        source_audit = next(
            row
            for row in self.audit_rows(repo)
            if row["auditType"] == "repo_state_snapshot" and row.get("payload", {}).get("snapshotHash") == snapshot["stateLedger"]["snapshotHash"]
        )
        manifest_dir = repo / ".claude-state" / "closeout" / "rollback"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        base_manifest = {
            "schema": "closeout-rollback-manifest.v1",
            "targetHead": snapshot["branch"]["head"],
            "sourceSnapshotPath": snapshot["stateLedger"]["historyPath"],
            "sourceSnapshotHash": snapshot["stateLedger"]["snapshotHash"],
            "sourceSnapshotAuditHash": source_audit["auditHash"],
            "repoClosedAuditHash": repo_closed_audit["auditHash"],
            "policyHash": config["policyHash"],
            "plannedStrategy": "git-revert",
            "userApproval": True,
            "recoveryCommand": "git revert --no-edit " + snapshot["branch"]["head"],
        }

        def write_manifest(name: str, updates: dict) -> str:
            payload = dict(base_manifest)
            payload.update(updates)
            path = manifest_dir / name
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return ".claude-state/closeout/rollback/" + name

        with self.assertRaisesRegex(HygieneError, "latest/current repo-state feeds"):
            validate_rollback_manifest(repo, write_manifest("latest.json", {"sourceSnapshotPath": snapshot["stateLedger"]["latestPath"]}))

        with self.assertRaisesRegex(HygieneError, "plannedStrategy is forbidden"):
            validate_rollback_manifest(repo, write_manifest("forbidden.json", {"plannedStrategy": "reset-hard"}))

        with self.assertRaisesRegex(HygieneError, "recoveryCommand contains forbidden action: reset-hard"):
            validate_rollback_manifest(repo, write_manifest("forbidden-command.json", {"recoveryCommand": "git reset --hard HEAD~1"}))

        with self.assertRaisesRegex(HygieneError, "recoveryCommand contains forbidden action: force-push"):
            validate_rollback_manifest(repo, write_manifest("force-refspec.json", {"recoveryCommand": "git push origin +HEAD:master"}))

        with self.assertRaisesRegex(HygieneError, "recoveryCommand contains forbidden action: force-push"):
            validate_rollback_manifest(repo, write_manifest("force-short-refspec.json", {"recoveryCommand": "git push origin +main"}))

        with self.assertRaisesRegex(HygieneError, "targetHead is stale"):
            validate_rollback_manifest(repo, write_manifest("stale.json", {"targetHead": "0" * 40}))

        with self.assertRaisesRegex(HygieneError, "policyHash mismatch"):
            validate_rollback_manifest(repo, write_manifest("policy.json", {"policyHash": "stale-policy"}))

        with self.assertRaisesRegex(HygieneError, "repoClosedAuditHash not found"):
            validate_rollback_manifest(repo, write_manifest("audit.json", {"repoClosedAuditHash": "missing"}))

        with self.assertRaisesRegex(HygieneError, "sourceSnapshotAuditHash not found"):
            validate_rollback_manifest(repo, write_manifest("source-audit.json", {"sourceSnapshotAuditHash": "missing"}))

        mismatched_source = json.loads((repo / snapshot["stateLedger"]["historyPath"]).read_text(encoding="utf-8"))
        mismatched_source["branch"]["head"] = "1" * 40
        mismatched_source["stateLedger"]["snapshotHash"] = stable_hash(
            {
                key: (
                    {inner_key: inner_value for inner_key, inner_value in value.items() if inner_key not in {"snapshotHash", "historyPath"}}
                    if key == "stateLedger" and isinstance(value, dict)
                    else value
                )
                for key, value in mismatched_source.items()
            }
        )
        mismatched_path = repo / snapshot["stateLedger"]["historyRoot"] / "mismatched-head.json"
        mismatched_path.write_text(json.dumps(mismatched_source, indent=2), encoding="utf-8")
        write_audit(
            repo,
            config,
            "repo_state_snapshot",
            {
                "snapshotHash": mismatched_source["stateLedger"]["snapshotHash"],
                "latestPath": mismatched_source["stateLedger"]["latestPath"],
                "historyPath": str(mismatched_path.relative_to(repo)).replace("\\", "/"),
                "writeMode": "latest-and-history",
                "branch": mismatched_source["branch"],
                "dirtyEntryCount": mismatched_source["dirty"]["entryCount"],
                "stashCount": len(mismatched_source["stashes"]),
                "worktreeCount": len(mismatched_source["worktrees"]),
                "linkedSiblingWorktreeCount": mismatched_source["worktreeInspection"]["ordinaryLinkedWorktreeCount"],
                "worktreeInspectionFailureCount": len(mismatched_source["worktreeInspection"]["inspectionFailures"]),
            },
            work_block_id="wb-rollback",
            outcome="success",
        )
        mismatched_audit = next(
            row
            for row in self.audit_rows(repo)
            if row["auditType"] == "repo_state_snapshot" and row.get("payload", {}).get("snapshotHash") == mismatched_source["stateLedger"]["snapshotHash"]
        )
        with self.assertRaisesRegex(HygieneError, "source snapshot branch head mismatch"):
            validate_rollback_manifest(
                repo,
                write_manifest(
                    "mismatched-head.json",
                    {
                        "sourceSnapshotPath": str(mismatched_path.relative_to(repo)).replace("\\", "/"),
                        "sourceSnapshotHash": mismatched_source["stateLedger"]["snapshotHash"],
                        "sourceSnapshotAuditHash": mismatched_audit["auditHash"],
                    },
                ),
            )

        outside_manifest = repo / ".claude-state" / "outside-rollback.json"
        outside_manifest.write_text(json.dumps(base_manifest, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(HygieneError, "rollback manifest path must stay under"):
            validate_rollback_manifest(repo, ".claude-state/outside-rollback.json")

        with self.assertRaisesRegex(HygieneError, "sourceSnapshotPath must stay under immutable repo-state history"):
            validate_rollback_manifest(
                repo,
                write_manifest("outside-source.json", {"sourceSnapshotPath": ".claude-state/closeout/manual-prune/source.json"}),
            )

        tampered_source = json.loads((repo / snapshot["stateLedger"]["historyPath"]).read_text(encoding="utf-8"))
        tampered_source["dirty"]["entryCount"] = 99
        tampered_path = repo / snapshot["stateLedger"]["historyRoot"] / "tampered.json"
        tampered_path.write_text(json.dumps(tampered_source, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(HygieneError, "declared hash mismatch"):
            validate_rollback_manifest(
                repo,
                write_manifest(
                    "tampered.json",
                    {
                        "sourceSnapshotPath": str(tampered_path.relative_to(repo)).replace("\\", "/"),
                        "sourceSnapshotHash": tampered_source["stateLedger"]["snapshotHash"],
                    },
                ),
            )

        audit_log = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        audit_rows = self.audit_rows(repo)
        for row in audit_rows:
            if row["auditType"] == "repo_state_snapshot" and row.get("payload", {}).get("snapshotHash") == snapshot["stateLedger"]["snapshotHash"]:
                row["payload"]["dirtyEntryCount"] = 777
                break
        audit_log.write_text("\n".join(json.dumps(row, sort_keys=True) for row in audit_rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(HygieneError, "source snapshot audit hash mismatch"):
            validate_rollback_manifest(repo, write_manifest("tampered-audit.json", {}))

        pwsh = shutil.which("pwsh.exe")
        if pwsh:
            wrapper = subprocess.run(
                [
                    pwsh,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "tools" / "closeout" / "validate-rollback-manifest.ps1"),
                    "-RepoRoot",
                    str(repo),
                    "-ManifestPath",
                    ".claude-state/closeout/rollback/missing.json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(wrapper.returncode, 0)

    @unittest.skipUnless(os.name == "nt", "PowerShell dashboard commands are Windows-specific")
    def test_closeout_dashboard_actions_are_read_only_and_owned(self) -> None:
        repo = self.init_repo(remote=True)

        latest = latest_repo_state_payload(repo)
        history = history_index_payload(repo)
        def pwsh_available(name):
            return "C:/Program Files/PowerShell/7/pwsh.exe" if name == "pwsh.exe" else None

        with mock.patch("tools.repo_hygiene.brokered_closeout.os.name", "nt"), mock.patch("tools.repo_hygiene.brokered_closeout.shutil.which", side_effect=pwsh_available):
            actions = dashboard_actions_payload(repo, server_process_id=1234)

        self.assertEqual(latest["stateLedger"]["writeMode"], "latest-only")
        self.assertEqual(history["schema"], "closeout-history-index.v1")
        self.assertEqual(actions["schema"], "closeout-dashboard-actions.v1")
        self.assertEqual(actions["status"], "ready")
        self.assertEqual(actions["serverProcessId"], 1234)
        self.assertTrue(os.path.samefile(actions["repoRoot"], repo))
        self.assertEqual(actions["dashboard"]["stickyUrlPath"], "/closeout")
        self.assertEqual(actions["dashboard"]["mutationModel"], "symbolic-action-request-only")
        self.assertEqual(actions["dashboard"]["duplicateLaunchPolicy"], "reuse-same-repo-fail-foreign-owner")
        self.assertEqual(actions["helper"]["scriptPath"], "tools\\closeout\\start-closeout-dashboard.ps1")
        self.assertEqual(actions["helper"]["serverProcessIdSource"], "/api/closeout/actions")
        self.assertEqual(actions["endpoints"]["page"], "/closeout")
        self.assertEqual(actions["endpoints"]["latest"], "/api/closeout/repo-state/latest")
        self.assertEqual(actions["endpoints"]["actionsPreview"], "/api/closeout/actions/preview")
        self.assertEqual(actions["endpoints"]["actionsRequest"], "/api/closeout/actions/request")
        self.assertEqual(actions["endpoints"]["actionsRequestHistory"], "/api/closeout/actions/requests")
        self.assertIn("delete-evidence", actions["forbiddenActions"])
        self.assertIn("force-push", actions["forbiddenActions"])
        self.assertEqual(actions["dashboard"]["refreshCommandPolicy"], "repo-owned-write-repo-state-latest-only")
        by_id = {item["id"]: item for item in actions["symbolicActions"]}
        self.assertEqual(by_id["refresh_repo_state"]["actionability"], "generated-feed-only")
        self.assertTrue(by_id["refresh_repo_state"]["previewAvailable"])
        self.assertEqual(by_id["refresh_repo_state"]["previewEndpoint"], "/api/closeout/actions/preview")
        self.assertEqual(by_id["refresh_repo_state"]["commandPolicy"], "repo-owned-write-repo-state-latest-only")
        self.assertFalse(by_id["refresh_repo_state"]["writesHistory"])
        self.assertTrue(by_id["refresh_repo_state"]["command"].startswith("pwsh.exe -NoLogo -NoProfile -NonInteractive"))
        self.assertTrue(by_id["request_retained_remediation"]["command"].startswith("pwsh.exe -NoLogo -NoProfile -NonInteractive"))
        self.assertEqual(by_id["request_retained_remediation"]["exactTupleRequired"], ["candidateId", "actionId", "evidenceHash", "policyHash", "pinnedRefs"])
        self.assertEqual(by_id["request_rollback"]["actionability"], "read-only-no-actor")
        self.assertIn("rollback actor has not validated", by_id["request_rollback"]["readinessReason"])
        self.assertEqual(by_id["request_rollback"]["requiredManifestSchema"], "closeout-rollback-manifest.v1")
        self.assertIn("validate-rollback-manifest.ps1", by_id["request_rollback"]["validatorCommand"])
        self.assertEqual(by_id["request_rollback"]["validatorActionability"], "read-only-validator")
        self.assertFalse(by_id["request_rollback"]["actionActorAvailable"])
        self.assertFalse(by_id["request_rollback"]["mutationReady"])
        self.assertIn("sourceSnapshotPath", by_id["request_rollback"]["exactTupleRequired"])
        self.assertIn("sourceSnapshotAuditHash", by_id["request_rollback"]["exactTupleRequired"])
        self.assertIn("repoClosedAuditHash", by_id["request_rollback"]["exactTupleRequired"])
        self.assertIn("recoveryCommand", by_id["request_rollback"]["exactTupleRequired"])

    def test_closeout_dashboard_action_preview_explains_current_blockers_without_mutation(self) -> None:
        repo = self.init_repo(remote=True)
        (repo / "dashboard-dirty.txt").write_text("dirty preview\n", encoding="utf-8")
        audit_path = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        repo_closed_path = repo / ".claude-state" / "closeout" / "repo-closed" / "repo.json"
        audit_before = audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []

        preview = dashboard_action_preview_payload(repo, {"actionId": "request_retained_remediation"})

        self.assertEqual(preview["schema"], "closeout-dashboard-action-preview.v1")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["actionId"], "request_retained_remediation")
        self.assertEqual(preview["previewMode"], "read-only-explain-and-dry-run")
        self.assertTrue(preview["previewToken"])
        self.assertEqual(preview["requestTemplate"]["previewToken"], preview["previewToken"])
        self.assertEqual(preview["requestTemplate"]["previewRepoStateHash"], preview["repoStateHash"])
        self.assertTrue(preview["noDirectMutation"])
        self.assertFalse(preview["wouldMutateNow"])
        self.assertEqual(preview["repoClosedPostcondition"]["status"], "blocked")
        blocker_kinds = {row["kind"] for row in preview["blockerSummary"]}
        self.assertIn("non_exempt_dirty_files", blocker_kinds)
        self.assertEqual(preview["candidateReports"]["reportRoot"], ".claude-state/closeout/repo-sweep/candidate-reports")
        self.assertFalse(repo_closed_path.exists())
        audit_after = audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []
        self.assertEqual(audit_after, audit_before)

    def test_closeout_dashboard_action_preview_reports_rollback_readiness(self) -> None:
        repo = self.init_repo(remote=True)

        preview = dashboard_action_preview_payload(repo, {"actionId": "request_rollback"})

        self.assertEqual(preview["schema"], "closeout-dashboard-action-preview.v1")
        self.assertEqual(preview["actionId"], "request_rollback")
        self.assertTrue(preview["previewToken"])
        self.assertFalse(preview["wouldMutateNow"])
        self.assertIn("Rollback is ultimately a mutating action", preview["explanation"])
        self.assertEqual(preview["rollback"]["requiredManifestSchema"], "closeout-rollback-manifest.v1")
        self.assertIn("sourceSnapshotPath", preview["rollback"]["requiredManifestFields"])
        self.assertEqual(preview["rollback"]["readiness"]["actionability"], "read-only-no-actor")
        self.assertFalse(preview["rollback"]["readiness"]["latestFeedIsRollbackEvidence"])

    def _dashboard_bound_request(
        self,
        repo: Path,
        *,
        action_id: str = "request_retained_remediation",
        exact_tuple: dict | None = None,
        server_process_id: int = 1234,
        helper_observed_at_ms: int | None = None,
        user_intent: str = "",
    ) -> dict:
        preview = dashboard_action_preview_payload(repo, {"actionId": action_id})
        return {
            "actionId": action_id,
            "serverProcessId": server_process_id,
            "helperObservedAtMs": int(time.time() * 1000) if helper_observed_at_ms is None else helper_observed_at_ms,
            "previewToken": preview["previewToken"],
            "previewRepoStateHash": preview["repoStateHash"],
            "exactTuple": exact_tuple if exact_tuple is not None else {},
            "userIntent": user_intent,
        }

    def test_closeout_dashboard_action_request_writes_packet_without_mutation(self) -> None:
        repo = self.init_repo(remote=True)
        branches_before = git(repo, "branch", "--format=%(refname:short)").stdout.splitlines()
        request = self._dashboard_bound_request(
            repo,
            exact_tuple={
                "candidateId": "candidate:retained",
                "actionId": "repo_sweep_prune_merged",
                "evidenceHash": "evidence",
                "policyHash": "policy",
                "pinnedRefs": {"target": "HEAD"},
            },
            user_intent="dashboard smoke request",
        )

        packet = dashboard_action_request_payload(repo, request, server_process_id=1234)

        self.assertEqual(packet["schema"], "closeout-dashboard-action-request.v1")
        self.assertEqual(packet["status"], "recorded")
        self.assertTrue(packet["noDirectMutation"])
        self.assertEqual(packet["mutationBoundary"], "repo-owned symbolic actors only")
        self.assertEqual(packet["actionId"], "request_retained_remediation")
        self.assertEqual(packet["helperFreshness"]["fresh"], True)
        self.assertEqual(packet["exactTuple"], request["exactTuple"])
        packet_path = repo / packet["requestPath"]
        self.assertTrue(packet_path.exists())
        persisted = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["repoStateHash"], packet["repoStateHash"])
        self.assertEqual(git(repo, "branch", "--format=%(refname:short)").stdout.splitlines(), branches_before)
        status_paths = [
            line[3:]
            for line in git(repo, "status", "--short").stdout.splitlines()
            if line and not line[3:].startswith(".claude-state/")
        ]
        self.assertEqual(status_paths, [])

    def test_closeout_dashboard_action_request_history_lists_recent_requests(self) -> None:
        repo = self.init_repo(remote=True)
        request_1 = self._dashboard_bound_request(
            repo,
            exact_tuple={
                "candidateId": "candidate:retained",
                "actionId": "repo_sweep_prune_merged",
                "evidenceHash": "evidence",
                "policyHash": "policy",
                "pinnedRefs": {"target": "HEAD"},
            },
            user_intent="history smoke request 1",
        )
        request_2 = dict(request_1)
        request_2["userIntent"] = "history smoke request 2"
        request_2["helperObservedAtMs"] = int(time.time() * 1000) + 500

        packet_1 = dashboard_action_request_payload(repo, request_1, server_process_id=1234)
        packet_2 = dashboard_action_request_payload(repo, request_2, server_process_id=1234)

        history = dashboard_action_request_history_payload(repo)

        self.assertEqual(history["schema"], "closeout-dashboard-action-request-history.v1")
        self.assertEqual(history["status"], "ready")
        self.assertTrue(history["immutable"])
        self.assertEqual(history["requestCount"], 2)
        self.assertEqual(history["displayedRequestCount"], 2)
        self.assertEqual(history["totalRequestCount"], 2)
        self.assertEqual(history["malformedCount"], 0)
        self.assertFalse(history["truncated"])
        self.assertGreaterEqual(len(history["entries"]), 2)
        history_request_ids = [entry["requestId"] for entry in history["entries"][:2]]
        self.assertIn(packet_1["requestId"], history_request_ids)
        self.assertIn(packet_2["requestId"], history_request_ids)
        self.assertIn("requestPath", history["entries"][0])
        self.assertIn("requestHash", history["entries"][0])
        self.assertEqual(history["entries"][0]["actionId"], "request_retained_remediation")
        self.assertEqual(history["entries"][0]["requestId"], packet_2["requestId"])

    def test_closeout_dashboard_action_request_history_surfaces_malformed_and_truncated_rows(self) -> None:
        repo = self.init_repo(remote=True)
        for index in range(30):
            dashboard_action_request_payload(
                repo,
                self._dashboard_bound_request(
                    repo,
                    helper_observed_at_ms=int(time.time() * 1000),
                    exact_tuple={
                        "candidateId": f"candidate:{index}",
                        "actionId": "repo_sweep_prune_merged",
                        "evidenceHash": f"evidence-{index}",
                        "policyHash": "policy",
                        "pinnedRefs": {"target": "HEAD"},
                    },
                    user_intent=f"history request {index}",
                ),
                server_process_id=1234,
            )
        request_root = repo / ".claude-state" / "closeout" / "dashboard-action-requests"
        malformed_path = request_root / "zz-malformed-request.json"
        malformed_path.write_text("{not-json", encoding="utf-8")
        os.utime(malformed_path, None)

        history = dashboard_action_request_history_payload(repo)

        self.assertEqual(history["schema"], "closeout-dashboard-action-request-history.v1")
        self.assertEqual(history["status"], "partial")
        self.assertTrue(history["immutable"])
        self.assertEqual(history["displayedRequestCount"], MAX_DASHBOARD_ACTION_REQUESTS)
        self.assertEqual(history["requestCount"], MAX_DASHBOARD_ACTION_REQUESTS)
        self.assertEqual(history["totalRequestCount"], 31)
        self.assertTrue(history["truncated"])
        self.assertEqual(history["malformedCount"], 1)
        malformed_rows = [entry for entry in history["entries"] if entry.get("status") == "malformed"]
        self.assertEqual(len(malformed_rows), 1)
        self.assertEqual(malformed_rows[0]["requestPath"], ".claude-state/closeout/dashboard-action-requests/zz-malformed-request.json")
        self.assertTrue(malformed_rows[0]["requestHash"])

    def test_closeout_dashboard_action_request_rejects_stale_or_unknown_action(self) -> None:
        repo = self.init_repo(remote=True)
        tuple_payload = {
            "candidateId": "candidate:retained",
            "actionId": "repo_sweep_prune_merged",
            "evidenceHash": "evidence",
            "policyHash": "policy",
            "pinnedRefs": {"target": "HEAD"},
        }
        bound_request = self._dashboard_bound_request(repo, exact_tuple=tuple_payload)

        with self.assertRaisesRegex(HygieneError, "stale dashboard helper state"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "serverProcessId": 4321,
                    "helperObservedAtMs": 0,
                },
                server_process_id=4321,
            )
        with self.assertRaisesRegex(HygieneError, "unsupported dashboard symbolic action request"):
            dashboard_action_request_payload(repo, {"actionId": "delete_everything"}, server_process_id=4321)
        with self.assertRaisesRegex(HygieneError, "missing exact tuple fields"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "serverProcessId": 4321,
                    "exactTuple": {},
                },
                server_process_id=4321,
            )

    def test_closeout_dashboard_action_request_rejects_missing_future_or_malformed_helper_evidence(self) -> None:
        repo = self.init_repo(remote=True)
        tuple_payload = {
            "candidateId": "candidate:retained",
            "actionId": "repo_sweep_prune_merged",
            "evidenceHash": "evidence",
            "policyHash": "policy",
            "pinnedRefs": {"target": "HEAD"},
        }
        now_ms = int(time.time() * 1000)
        bound_request = self._dashboard_bound_request(
            repo,
            exact_tuple=tuple_payload,
            server_process_id=4321,
            helper_observed_at_ms=now_ms,
        )

        with self.assertRaisesRegex(HygieneError, "missing serverProcessId"):
            dashboard_action_request_payload(
                repo,
                {key: value for key, value in bound_request.items() if key != "serverProcessId"},
                server_process_id=4321,
            )
        with self.assertRaisesRegex(HygieneError, "missing helperObservedAtMs"):
            dashboard_action_request_payload(
                repo,
                {key: value for key, value in bound_request.items() if key != "helperObservedAtMs"},
                server_process_id=4321,
            )
        # Freeze the validator clock so this negative cannot age into the
        # accepted window while a contended repo-state snapshot is prepared.
        with mock.patch("tools.repo_hygiene.closeout_dashboard.time.time", return_value=now_ms / 1000.0):
            with self.assertRaisesRegex(HygieneError, "timestamp is in the future"):
                dashboard_action_request_payload(
                    repo,
                    {
                        **bound_request,
                        "helperObservedAtMs": now_ms + 60000,
                    },
                    server_process_id=4321,
                )
        with self.assertRaisesRegex(HygieneError, "serverProcessId must be an integer"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "serverProcessId": "not-a-pid",
                },
                server_process_id=4321,
            )
        with self.assertRaisesRegex(HygieneError, "serverProcessId must be an integer"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "serverProcessId": 4321.9,
                },
                server_process_id=4321,
            )
        with self.assertRaisesRegex(HygieneError, "stale dashboard helper process id"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "serverProcessId": 1235,
                    "observedServerProcessId": 4321,
                },
                server_process_id=4321,
            )
        with self.assertRaisesRegex(HygieneError, "helperObservedAtMs must be an integer"):
            dashboard_action_request_payload(
                repo,
                {
                    **bound_request,
                    "helperObservedAtMs": "not-a-time",
                },
                server_process_id=4321,
            )
        fresh_now_ms = int(time.time() * 1000)
        fresh_bound_request = {
            **bound_request,
            "helperObservedAtMs": fresh_now_ms,
        }
        # Each negative targets preview evidence, not helper freshness. Freeze
        # the validator clock so a contended repo snapshot cannot age the
        # request into a different fail-closed branch first.
        with mock.patch("tools.repo_hygiene.closeout_dashboard.time.time", return_value=fresh_now_ms / 1000.0):
            with self.assertRaisesRegex(HygieneError, "missing previewToken"):
                dashboard_action_request_payload(
                    repo,
                    {key: value for key, value in fresh_bound_request.items() if key != "previewToken"},
                    server_process_id=4321,
                )
            with self.assertRaisesRegex(HygieneError, "stale dashboard preview repo state"):
                dashboard_action_request_payload(
                    repo,
                    {
                        **fresh_bound_request,
                        "previewRepoStateHash": "stale-hash",
                    },
                    server_process_id=4321,
                )
            with self.assertRaisesRegex(HygieneError, "stale dashboard preview token"):
                dashboard_action_request_payload(
                    repo,
                    {
                        **fresh_bound_request,
                        "previewToken": "stale-token",
                    },
                    server_process_id=4321,
                )

    def test_closeout_dashboard_action_request_rejects_empty_tuple_values_and_path_escape(self) -> None:
        repo = self.init_repo(
            remote=True,
            config_updates={"webDashboardSpec": {"actionRequestRoot": ".claude-state/../../outside-action-requests"}},
        )
        request = self._dashboard_bound_request(
            repo,
            exact_tuple={
                "candidateId": "candidate:retained",
                "actionId": "repo_sweep_prune_merged",
                "evidenceHash": "evidence",
                "policyHash": "",
                "pinnedRefs": {"target": "HEAD"},
            },
        )

        with self.assertRaisesRegex(HygieneError, "missing exact tuple fields: policyHash"):
            dashboard_action_request_payload(repo, request, server_process_id=1234)
        request["exactTuple"]["policyHash"] = "policy"
        request["exactTuple"]["pinnedRefs"] = {"target": ""}
        with self.assertRaisesRegex(HygieneError, "missing exact tuple fields: pinnedRefs"):
            dashboard_action_request_payload(repo, request, server_process_id=1234)
        request["exactTuple"]["pinnedRefs"] = {"target": "HEAD"}
        request["exactTuple"]["policyHash"] = "policy"
        # The preceding malformed-tuple calls may be expensive on a loaded host;
        # refresh only the helper observation so this assertion reaches the
        # independently targeted path-containment guard.
        request["helperObservedAtMs"] = int(time.time() * 1000)
        with self.assertRaisesRegex(HygieneError, "dashboard action request root must stay under"):
            dashboard_action_request_payload(repo, request, server_process_id=1234)
        self.assertFalse((self.tempdir / "outside-action-requests").exists())

    def test_closeout_dashboard_rejects_malformed_config_numbers(self) -> None:
        repo_stale = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"helper": {"staleAfterMs": "soon"}}})
        with self.assertRaisesRegex(HygieneError, "helper.staleAfterMs must be an integer"):
            dashboard_actions_payload(repo_stale, server_process_id=1234)

        repo_port = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"helper": {"port": 8765.5}}})
        with self.assertRaisesRegex(HygieneError, "helper.port must be an integer"):
            dashboard_actions_payload(repo_port, server_process_id=1234)

        repo_false_port = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"helper": {"port": False}}})
        with self.assertRaisesRegex(HygieneError, "helper.port must be an integer"):
            dashboard_actions_payload(repo_false_port, server_process_id=1234)
        with self.assertRaisesRegex(HygieneError, "helper.port must be an integer"):
            repo_state_snapshot(repo_false_port, write=False)
        repo_zero_port = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"helper": {"port": 0}}})
        with self.assertRaisesRegex(HygieneError, "helper.port must be >= 1"):
            dashboard_actions_payload(repo_zero_port, server_process_id=1234)

        repo_refresh = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"autoRefreshMs": "fast"}})
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be an integer"):
            dashboard_actions_payload(repo_refresh, server_process_id=1234)
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be an integer"):
            dashboard_html(load_closeout_config(repo_refresh))

        repo_empty_refresh = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"autoRefreshMs": ""}})
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be an integer"):
            dashboard_actions_payload(repo_empty_refresh, server_process_id=1234)
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be an integer"):
            repo_state_snapshot(repo_empty_refresh, write=False)
        repo_zero_refresh = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"autoRefreshMs": 0}})
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be >= 1000"):
            dashboard_actions_payload(repo_zero_refresh, server_process_id=1234)
        with self.assertRaisesRegex(HygieneError, "dashboard.autoRefreshMs must be >= 1000"):
            repo_state_snapshot(repo_zero_refresh, write=False)
        repo_huge_stale = self.init_repo(remote=True, config_updates={"webDashboardSpec": {"helper": {"staleAfterMs": 999999999999}}})
        with self.assertRaisesRegex(HygieneError, "helper.staleAfterMs must be <= 60000"):
            dashboard_actions_payload(repo_huge_stale, server_process_id=1234)
        with self.assertRaisesRegex(HygieneError, "helper.staleAfterMs must be <= 60000"):
            repo_state_snapshot(repo_huge_stale, write=False)

    def test_closeout_dashboard_history_snapshot_rejects_path_escape(self) -> None:
        repo = self.init_repo(remote=True)
        snapshot = repo_state_snapshot(repo, write=True, work_block_id="wb-dashboard")
        history_id = Path(snapshot["stateLedger"]["historyPath"]).name

        served = history_snapshot_payload(repo, history_id)

        self.assertEqual(served["stateLedger"]["servedHistorySnapshotId"], history_id)
        self.assertEqual(served["artifactSchema"], "repo-state-snapshot.v1")
        with self.assertRaises(HygieneError):
            history_snapshot_payload(repo, "../closeout.config")
        with self.assertRaises(HygieneError):
            history_snapshot_payload(repo, "%2e%2e%2fcloseout.config")

    def test_closeout_dashboard_page_uses_sse_with_polling_fallback(self) -> None:
        repo = self.init_repo(remote=True)
        page = dashboard_html(load_closeout_config(repo))

        self.assertIn("new EventSource", page)
        self.assertIn('source.addEventListener("repo-state"', page)
        self.assertIn("startPolling", page)
        self.assertIn("window.setInterval", page)
        self.assertIn("/api/closeout/events", page)
        self.assertIn("/api/closeout/actions/preview", page)
        self.assertIn("/api/closeout/actions/request", page)
        self.assertIn("/api/closeout/actions/requests", page)
        self.assertIn("Queue symbolic request", page)
        self.assertIn("loadActionPreview", page)
        self.assertIn("Preview</button>", page)
        self.assertIn("action-request-history-summary", page)
        self.assertIn("action-request-history-table", page)
        self.assertIn("selectedActionIdFromUrl", page)
        self.assertIn("actionId", page)

    def test_closeout_dashboard_page_preserves_configured_client_state_keys(self) -> None:
        repo = self.init_repo(remote=True)
        page = dashboard_html(load_closeout_config(repo))

        self.assertIn("data-preserved-client-state-keys", page)
        self.assertIn("preservedClientStateKeys", page)
        self.assertIn("scrollPosition", page)
        self.assertIn("focusedElement", page)
        self.assertIn("selectedWorkBlockId", page)
        self.assertIn("expandedRows", page)
        self.assertIn("activeHistoryFilters", page)
        self.assertIn("configuredClientState", page)

    def test_dashboard_refresh_command_rejects_unsupported_configured_command(self) -> None:
        repo = self.init_repo(
            remote=True,
            config_updates={
                "repoStateLedger": {
                    "liveRefreshCommand": "custom-refresh --repo .",
                },
                "webDashboardSpec": {
                    "refreshCommand": "custom-refresh --repo .",
                },
            },
        )

        with self.assertRaisesRegex(HygieneError, "unsupported configured closeout command"):
            repo_state_snapshot(repo, write=False)
        with self.assertRaisesRegex(HygieneError, "unsupported configured closeout command"):
            dashboard_actions_payload(repo)

    def test_repo_state_dashboard_and_rollback_contract_required(self) -> None:
        config = load_closeout_config(ROOT)
        baseline = config["toolingBaseline"]
        required_symbols = {(item["path"], item["contains"]) for item in baseline["requiredSymbols"]}
        agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude_text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        standard_text = (ROOT / "CLOSEOUT-STANDARD.md").read_text(encoding="utf-8")
        prompt_text = (ROOT / "CLOSEOUT-IMPLEMENTATION-PROMPT.md").read_text(encoding="utf-8")
        docs_text = (ROOT / "docs" / "18-automatic-work-block-closeout-standard.md").read_text(encoding="utf-8")
        dashboard_spec_text = (ROOT / "docs" / "19-closeout-dashboard-spec.md").read_text(encoding="utf-8")

        self.assertIn("repoStateLedger", baseline["requiredConfigKeys"])
        self.assertIn("webDashboardSpec", baseline["requiredConfigKeys"])
        self.assertIn("rollbackPolicy", baseline["requiredConfigKeys"])
        self.assertIn("rollbackPolicy.validationSchema", baseline["requiredConfigKeys"])
        self.assertIn("rollbackPolicy.manifestRoot", baseline["requiredConfigKeys"])
        self.assertIn("rollbackPolicy.validatorCommand", baseline["requiredConfigKeys"])
        self.assertIn("rollbackPolicy.validatorActionability", baseline["requiredConfigKeys"])
        self.assertIn("powerShell", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.preferredExecutable", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.requiredArgs", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.windowsPowerShellOnly", baseline["requiredConfigKeys"])
        self.assertIn("test_repo_state_snapshot_writes_dashboard_ready_ledger_and_audit", baseline["requiredTests"])
        self.assertIn("test_repo_state_dashboard_and_rollback_contract_required", baseline["requiredTests"])
        self.assertIn("test_powershell_policy_prefers_pwsh_no_profile_for_closeout_commands", baseline["requiredTests"])
        self.assertIn("test_closeout_tooling_stale_reports_missing_power_shell_policy", baseline["requiredTests"])
        self.assertIn("test_repo_state_latest_only_refresh_updates_feed_without_audit_noise", baseline["requiredTests"])
        self.assertIn("test_repo_state_snapshot_reports_worktree_inspection", baseline["requiredTests"])
        self.assertIn("test_repo_state_rollback_readiness_fails_closed_without_actor", baseline["requiredTests"])
        self.assertIn("test_validate_rollback_manifest_accepts_immutable_history_and_audit", baseline["requiredTests"])
        self.assertIn("test_validate_rollback_manifest_rejects_latest_forbidden_and_stale_evidence", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_actions_are_read_only_and_owned", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_preview_explains_current_blockers_without_mutation", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_preview_reports_rollback_readiness", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_writes_packet_without_mutation", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_rejects_stale_or_unknown_action", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_rejects_missing_future_or_malformed_helper_evidence", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_rejects_empty_tuple_values_and_path_escape", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_rejects_malformed_config_numbers", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_history_snapshot_rejects_path_escape", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_page_uses_sse_with_polling_fallback", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_history_lists_recent_requests", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_action_request_history_surfaces_malformed_and_truncated_rows", baseline["requiredTests"])
        self.assertIn("test_closeout_dashboard_page_preserves_configured_client_state_keys", baseline["requiredTests"])
        self.assertIn("test_dashboard_refresh_command_rejects_unsupported_configured_command", baseline["requiredTests"])
        self.assertIn("webDashboardSpec.readOnlyByDefault", baseline["requiredConfigKeys"])
        self.assertIn("webDashboardSpec.preserveClientStateAcrossRefresh", baseline["requiredConfigKeys"])
        self.assertIn("webDashboardSpec.rollbackForbiddenActions", baseline["requiredConfigKeys"])
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def repo_state_snapshot"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "closeout-history-index.v1"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "rollback-readiness.v1"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def repo_state_snapshot_evidence_hash"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def repo_closed_postcondition_state"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def recover_dirty_protected_target_to_work_block"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def recovery_command_forbidden_action"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def require_integrity_checked_audit"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def validate_rollback_manifest"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "validate-rollback-manifest"), required_symbols)
        self.assertIn(("tools/closeout/validate-rollback-manifest.ps1", "validate-rollback-manifest"), required_symbols)
        self.assertIn(("tools/closeout/validate-rollback-manifest.ps1", "exit $LASTEXITCODE"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def powershell_executable_for_policy"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def closeout_script_command"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def effective_closeout_script_command"), required_symbols)
        self.assertIn(("tools/agent-bridge/powershell_runtime.py", "def powershell_cim_command"), required_symbols)
        self.assertIn(("tools/agent-bridge/bootstrap_session.py", "powershell_cim_command"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "DASHBOARD_ACTIONS_SCHEMA"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "DASHBOARD_ACTION_PREVIEW_SCHEMA"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "DASHBOARD_ACTION_REQUEST_SCHEMA"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "DASHBOARD_ACTION_REQUEST_HISTORY_SCHEMA"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "def dashboard_actions_payload"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "def dashboard_action_preview_payload"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "def dashboard_action_request_payload"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "def dashboard_action_request_history_payload"), required_symbols)
        self.assertIn(("tools/repo_hygiene/closeout_dashboard.py", "def history_snapshot_payload"), required_symbols)
        self.assertIn(("docs/19-closeout-dashboard-spec.md", "workflow-comparison"), required_symbols)
        self.assertIn(("docs/19-closeout-dashboard-spec.md", "round-delta note"), required_symbols)
        self.assertIn(("docs/19-closeout-dashboard-spec.md", "read-first"), required_symbols)
        self.assertIn(("docs/19-closeout-dashboard-spec.md", "same work block"), required_symbols)
        self.assertIn(("docs/19-closeout-dashboard-spec.md", "freshness"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "repo-state"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "--latest-only"), required_symbols)
        self.assertIn(("tools/closeout/write-repo-state.ps1", "LatestOnly"), required_symbols)
        self.assertIn(("tools/closeout/start-closeout-dashboard.ps1", "serverProcessId"), required_symbols)
        self.assertIn(("tools/closeout/start-closeout-dashboard.ps1", "reuse"), required_symbols)
        self.assertIn(("AGENTS.md", "repoStateLedger"), required_symbols)
        self.assertIn(("AGENTS.md", "PowerShell 7+"), required_symbols)
        self.assertIn(("CLAUDE.md", "rollbackPolicy"), required_symbols)
        self.assertIn(("CLAUDE.md", "PowerShell 7+"), required_symbols)
        self.assertIn(("closeout.config.json", "preferredExecutable"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def worktree_inspection_state"), required_symbols)
        for text in (agents_text, claude_text, standard_text, docs_text):
            self.assertIn("repoStateLedger", text)
            self.assertIn("webDashboardSpec", text)
            self.assertIn("rollbackPolicy", text)
            self.assertIn("worktree-inspection.v1", text)
            self.assertIn("start-closeout-dashboard.ps1", text)
            self.assertIn("/api/closeout/actions", text)
            self.assertIn("/api/closeout/actions/preview", text)
            self.assertIn("/api/closeout/actions/requests", text)
            self.assertIn("action-request-history", text)
            self.assertIn("dashboard-action-requests", text)
            self.assertIn("validate-rollback-manifest.ps1", text)
            self.assertIn("repoClosedAuditHash", text)
        self.assertIn("canonical dashboard contract", agents_text)
        self.assertIn("canonical dashboard contract", claude_text)
        self.assertIn("canonical phase matrix", standard_text)
        self.assertIn("workflow-comparison", dashboard_spec_text)
        self.assertIn("round-delta note", agents_text)
        self.assertIn("round-delta note", claude_text)
        self.assertIn("round-delta note", standard_text)
        self.assertIn("Cross-Repo Prompt Comparison", prompt_text)
        self.assertIn("same-work-block regeneration rule", prompt_text)
        self.assertIn("round-delta note", (ROOT / "CLOSEOUT-CROSS-MAP-COMPARISON.md").read_text(encoding="utf-8"))
        self.assertIn("same work block", dashboard_spec_text)
        self.assertIn("freshness", dashboard_spec_text)
        self.assertIn("cross-repo comparison", dashboard_spec_text)
        self.assertIn("closeout-compare-result.v1", dashboard_spec_text)
        self.assertIn("closeout-compare-result.v1", (ROOT / "CLOSEOUT-CROSS-MAP-COMPARISON.md").read_text(encoding="utf-8"))
        self.assertIn("closeout-compare-result.v1", (ROOT / "docs/18-automatic-work-block-closeout-standard.md").read_text(encoding="utf-8"))
        self.assertIn("closeout-compare-result.v1", (ROOT / "docs/20-closeout-commit-history-map.md").read_text(encoding="utf-8"))
        self.assertIn("repo_closed_for_final_response", claude_text)
        self.assertIn("repo_closed_for_final_response", dashboard_spec_text)
        self.assertIn("repo_closed_for_final_response", (ROOT / "CLOSEOUT-CROSS-MAP-COMPARISON.md").read_text(encoding="utf-8"))
        self.assertIn("repo_closed_for_final_response", (ROOT / "docs/20-closeout-commit-history-map.md").read_text(encoding="utf-8"))
        self.assertIn("closeout.compare-result.schema.json", dashboard_spec_text)
        self.assertIn("closeout.compare-result.schema.json", (ROOT / "docs/18-automatic-work-block-closeout-standard.md").read_text(encoding="utf-8"))
        self.assertIn("closeout.compare-result.schema.json", (ROOT / "docs/20-closeout-commit-history-map.md").read_text(encoding="utf-8"))
        self.assertIn("closeout-compare-result.json", (ROOT / "tools/repo-hygiene/closeout.contract.json").read_text(encoding="utf-8"))
        self.assertIn("closeout-compare-result.schema.json", (ROOT / "tools/repo-hygiene/closeout.contract.json").read_text(encoding="utf-8"))
        self.assertIn(("CLOSEOUT-CANONICAL-CONTRACT.md", "repo_closed_for_final_response"), required_symbols)
        self.assertIn(("CLOSEOUT-CANONICAL-CONTRACT.md", "closeout-compare-result.v1"), required_symbols)
        self.assertIn(("CLOSEOUT-CANONICAL-CONTRACT.md", "CLOSEOUT-IMPLEMENTATION-PROMPT.md"), required_symbols)
        self.assertIn("compare-result.json", dashboard_spec_text)
        self.assertIn("current", dashboard_spec_text)
        self.assertIn("stale", dashboard_spec_text)
        self.assertIn("divergent", dashboard_spec_text)
        self.assertIn("blocked", dashboard_spec_text)
        self.assertIn("read-first", dashboard_spec_text)
        self.assertIn("Inspect: evidence only", dashboard_spec_text)
        self.assertIn("Preview: explain consequences and blockers", dashboard_spec_text)
        self.assertIn("Request: record durable symbolic intent", dashboard_spec_text)
        self.assertIn("Apply: repo-owned actor only", dashboard_spec_text)
        self.assertIn("repo-state-snapshot.v1", dashboard_spec_text)
        self.assertIn("closeout-history-index.v1", dashboard_spec_text)
        self.assertIn("rollback-readiness.v1", dashboard_spec_text)
        self.assertIn("closeout-rollback-manifest-validation.v1", dashboard_spec_text)
        self.assertIn("worktree-inspection.v1", dashboard_spec_text)
        self.assertIn("symbolic-action-request-only", dashboard_spec_text)
        self.assertIn("write-repo-state.ps1", dashboard_spec_text)
        self.assertIn("start-closeout-dashboard.ps1", dashboard_spec_text)
        self.assertIn("pwsh.exe -NoLogo -NoProfile -NonInteractive", dashboard_spec_text)
        self.assertIn("/api/closeout/repo-state/latest", dashboard_spec_text)
        self.assertIn("/api/closeout/actions/preview", dashboard_spec_text)
        self.assertIn("/api/closeout/actions/request", dashboard_spec_text)
        self.assertIn("malformedCount", dashboard_spec_text)
        self.assertIn("truncated", dashboard_spec_text)
        self.assertIn("http://127.0.0.1:8765/closeout", dashboard_spec_text)

    def test_canonical_closeout_contract_block_is_pinned(self) -> None:
        contract_path = ROOT / "CLOSEOUT-CANONICAL-CONTRACT.md"
        sentinel_path = ROOT / "CLOSEOUT-CANONICAL-CONTRACT.sha256"
        self.assertTrue(contract_path.exists(), contract_path)
        self.assertTrue(sentinel_path.exists(), sentinel_path)

        contract_text = contract_path.read_text(encoding="utf-8")
        canonical_bytes = subprocess.run(
            ["git", "show", "HEAD:CLOSEOUT-CANONICAL-CONTRACT.md"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
        self.assertEqual(sentinel_path.read_text(encoding="utf-8").strip(), expected_hash)

        self.assertIn("byte-for-byte identical", contract_text)
        self.assertIn("CLOSEOUT-IMPLEMENTATION-PROMPT.md", contract_text)
        self.assertIn("CLOSEOUT-STANDARD.md", contract_text)
        self.assertIn("closeout-compare-result.v1", contract_text)
        self.assertIn("repo_closed_for_final_response", contract_text)
        self.assertIn("closeoutCleanTruth", contract_text)
        self.assertIn("CLOSEOUT-CROSS-MAP-COMPARISON.md", contract_text)
        self.assertIn("CLOSEOUT-REQUIREMENTS-TRACE.md", contract_text)

    def test_compare_result_artifact_shape_is_pinned(self) -> None:
        compare_result_path = ROOT / ".claude-state" / "closeout" / "workflow-comparison" / "compare-result.json"
        schema_path = ROOT / "tools" / "repo-hygiene" / "closeout.compare-result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["artifactType"]["const"], "closeout-compare-result.v1")
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], 1)
        if not compare_result_path.exists():
            self.skipTest("live compare-result artifact is generated and ignored on clean checkouts")
        compare_result = json.loads(compare_result_path.read_text(encoding="utf-8"))
        self.assertEqual(compare_result["artifactType"], "closeout-compare-result.v1")
        self.assertEqual(compare_result["schemaVersion"], 1)
        Draft202012Validator(schema).validate(compare_result)
        validate_compare_result_schema(compare_result)

    def test_capability_ledger_contains_frozen_row_inventory(self) -> None:
        ledger = json.loads((ROOT / "CLOSEOUT-CAPABILITY-LEDGER.json").read_text(encoding="utf-8"))
        rows = ledger["capabilities"]
        capability_ids = [row["capabilityId"] for row in rows]
        self.assertEqual(sorted(capability_ids), sorted(FROZEN_CLOSEOUT_CAPABILITY_ROWS))
        self.assertEqual(len(capability_ids), len(set(capability_ids)))
        status_summary = {key: 0 for key in ("YES", "PARTIAL", "NO", "UNAVAILABLE", "UNKNOWN")}
        for row in rows:
            status_summary[row["status"]] += 1
            if row["status"] != "YES":
                self.assertTrue(row["blockers"], row["capabilityId"])
                continue
            self.assertNotIn(row["verification"]["claimBasis"], {"documentation-only", "reported-only", "not-implemented", "unknown"})
            proof_paths = []
            for key in ("testPaths", "actorPaths", "adapterPaths", "configPaths", "contractPaths", "driftCheckPaths"):
                proof_paths.extend(row.get(key, []))
            self.assertTrue(proof_paths, row["capabilityId"])
            committed_proof = [
                path
                for path in proof_paths
                if git(ROOT, "ls-files", "--error-unmatch", path, check=False).returncode == 0
            ]
            self.assertTrue(committed_proof, row["capabilityId"])
        self.assertEqual(ledger["statusSummary"], status_summary)

    def test_contract_records_closeout_addendum_persistence_rule(self) -> None:
        config = load_closeout_config(ROOT)
        persistence = config["closeoutAddendumPersistence"]
        self.assertTrue(persistence["enabled"])
        self.assertTrue(persistence["sameTurnRequired"])
        self.assertIn("closeout addendum", persistence["incomingLabels"])
        self.assertIn("AGENTS.md", persistence["repoWideSurfaces"])
        self.assertIn("CLAUDE.md", persistence["repoWideSurfaces"])
        self.assertIn("closeout.config.json", persistence["repoWideSurfaces"])
        self.assertIn("test_or_tooling_baseline_guard", persistence["minimumDurableArtifacts"])
        claude_text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Hard-clean final responses are blocked unless the repo-closed postcondition passes", claude_text)
        baseline = config["toolingBaseline"]
        self.assertIn("workBlockBootstrap", baseline["requiredConfigKeys"])
        self.assertIn("workBlockBootstrap.requireIntegratedStartHeadForFinalize", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate.requireClaudeApprovalForFinalize", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate.coordinationFile", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate.handoffActor", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate.reviewActor", baseline["requiredConfigKeys"])
        self.assertIn("contentReviewGate.authorizedReviewSessions", baseline["requiredConfigKeys"])
        self.assertIn("closeoutAddendumPersistence", baseline["requiredConfigKeys"])
        self.assertIn("finalizeLoop", baseline["requiredConfigKeys"])
        self.assertIn("agentRemediation", baseline["requiredConfigKeys"])
        self.assertIn("agentRemediationQueue", baseline["requiredConfigKeys"])
        self.assertIn("hardClean", baseline["requiredConfigKeys"])
        self.assertIn("runtimeServices", baseline["requiredConfigKeys"])
        self.assertIn("locking", baseline["requiredConfigKeys"])
        self.assertIn("autoEligibilityRepair", baseline["requiredConfigKeys"])
        self.assertIn("processResources", baseline["requiredConfigKeys"])
        self.assertIn("powerShell", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.preferredExecutable", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.requiredArgs", baseline["requiredConfigKeys"])
        self.assertIn(
            "test_content_review_authorized_session_rotation_is_strict_singleton",
            baseline["requiredTests"],
        )
        self.assertIn("test_bounded_runner_kills_hung_finalize_child_with_descendants", baseline["requiredTests"])
        self.assertIn("test_start_work_block_auto_branches_from_clean_protected_target", baseline["requiredTests"])
        self.assertIn("test_start_work_block_blocks_dirty_protected_target_before_auto_branch", baseline["requiredTests"])
        self.assertIn("test_finalize_blocks_work_block_started_from_unintegrated_base", baseline["requiredTests"])
        self.assertIn("test_finalize_blocks_review_gated_chunk_without_claude_approval", baseline["requiredTests"])
        self.assertIn("test_finalize_rejects_claude_approval_for_different_range", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_caps_oversized_child_output", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_normalizes_known_failure_text_with_zero_exit", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_trusts_finalize_semantic_success_over_validation_text", baseline["requiredTests"])
        self.assertIn("test_hard_clean_final_response_blocks_non_exempt_dirty_files", baseline["requiredTests"])
        self.assertIn("test_hard_clean_final_response_blocks_remaining_stash", baseline["requiredTests"])
        self.assertIn("test_hard_clean_final_response_passes_after_clean_promotion", baseline["requiredTests"])
        self.assertIn("test_runtime_service_stops_before_validation_and_restarts_after_repo_closed", baseline["requiredTests"])
        self.assertIn("test_runtime_service_not_restarted_after_failed_validation_stale_refs_or_repo_closed_failure", baseline["requiredTests"])
        self.assertIn("test_closeout_tooling_stale_reports_missing_hard_clean_gate", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_reports_unified_closeout_clean_truth", baseline["requiredTests"])
        self.assertIn("test_closeout_clean_truth_preserves_raw_git_dirty_but_policy_clean_for_exempt_state", baseline["requiredTests"])
        self.assertIn("test_closeout_clean_truth_contract_required", baseline["requiredTests"])
        self.assertIn("test_closeout_adapter_heartbeat_is_contract_required", baseline["requiredTests"])
        self.assertIn("test_agent_remediation_queue_policy_fields_are_contract_required", baseline["requiredTests"])
        self.assertIn("test_codex_agent_queue_consumer_plans_one_background_agent_per_eligible_shard", baseline["requiredTests"])
        self.assertIn("test_agent_queue_dirty_hash_detects_same_status_byte_change", baseline["requiredTests"])
        self.assertIn("test_agent_queue_stale_packet_blocks_even_when_valid_shard_can_spawn", baseline["requiredTests"])
        self.assertIn("test_agent_queue_remote_fetch_failure_blocks_stale_check", baseline["requiredTests"])
        self.assertIn("test_agent_queue_retirement_remote_fetch_failure_blocks_without_retiring_packet", baseline["requiredTests"])
        self.assertIn("test_agent_queue_result_path_outside_result_root_is_stale", baseline["requiredTests"])
        self.assertIn("test_agent_result_collection_rejects_out_of_scope_changed_paths", baseline["requiredTests"])
        self.assertIn("test_agent_result_collection_returns_symbolic_next_action_without_mutation", baseline["requiredTests"])
        self.assertIn("test_agent_result_collection_blocks_agent_reported_blockers_and_failed_validation", baseline["requiredTests"])
        self.assertIn("test_agent_result_collection_rejects_missing_required_result_fields", baseline["requiredTests"])
        self.assertIn("test_agent_result_collection_rejects_wrong_shard_identity", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_blocks_pending_agent_remediation_queue", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_blocks_unreadable_or_unretired_queue_artifacts", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_blocks_invalid_queue_retirement_proof", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_accepts_valid_queue_retirement_proof", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_rejects_wrong_result_tuple", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_rejects_stale_collection_retirement_tuple", baseline["requiredTests"])
        self.assertIn("test_non_ancestor_historical_branch_prune_requires_bundle_backed_recovery", baseline["requiredTests"])
        self.assertIn("test_dirty_detached_worktree_removal_refuses_missing_byte_preservation", baseline["requiredTests"])
        self.assertIn("test_repo_sweep_generated_only_detached_dirty_worktree_is_pruned_with_proof", baseline["requiredTests"])
        self.assertIn("test_repo_sweep_large_real_detached_evidence_is_preserved_not_count_blocked", baseline["requiredTests"])
        self.assertIn("test_detached_dirty_preserve_copy_uses_extended_length_destination", baseline["requiredTests"])
        self.assertIn("test_recovery_audit_records_heads_hashes_and_reviewer_verdicts", baseline["requiredTests"])
        self.assertIn("test_stale_transaction_branch_pruned_after_recovery_evidence", baseline["requiredTests"])
        self.assertIn("test_final_repo_sweep_after_prune_reports_zero_candidates", baseline["requiredTests"])
        self.assertIn("test_deletion_tuple_rejects_missing_or_stale_recovery_artifact", baseline["requiredTests"])
        self.assertIn("test_dirty_protected_target_finalize_recovers_to_feature_branch", baseline["requiredTests"])
        required_symbols = {(item["path"], item["contains"]) for item in baseline["requiredSymbols"]}
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def closeout_process_resource_policy"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def apply_windows_process_tree_affinity"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def process_tree_cpu_sample"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def validate_work_block_start_head_integrated"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def validate_content_review_approval_for_finalize"), required_symbols)
        self.assertIn(("AGENTS.md", "Closeout actors must be bounded at the process boundary"), required_symbols)
        self.assertIn(("AGENTS.md", "Hard-clean final responses are blocked unless the repo-closed postcondition passes"), required_symbols)
        self.assertIn(("AGENTS.md", "closeoutCleanTruth"), required_symbols)
        self.assertIn(("AGENTS.md", "agentRemediationQueue.queueRoots"), required_symbols)
        self.assertIn(("AGENTS.md", "protected-target-noop-closeout"), required_symbols)
        self.assertIn(("AGENTS.md", "protected-target-dirty-recovery"), required_symbols)
        self.assertIn(("AGENTS.md", "workBlockBootstrap.autoBranchFromProtectedTarget"), required_symbols)
        self.assertIn(("AGENTS.md", "workBlockBootstrap.requireIntegratedStartHeadForFinalize"), required_symbols)
        self.assertIn(("CLAUDE.md", "Hard-clean final responses are blocked unless the repo-closed postcondition passes"), required_symbols)
        self.assertIn(("CLAUDE.md", "closeoutCleanTruth"), required_symbols)
        self.assertIn(("CLAUDE.md", "agentRemediationQueue.queueRoots"), required_symbols)
        self.assertIn(("CLAUDE.md", "protected-target-noop-closeout"), required_symbols)
        self.assertIn(("CLAUDE.md", "protected-target-dirty-recovery"), required_symbols)
        self.assertIn(("CLAUDE.md", "workBlockBootstrap.autoBranchFromProtectedTarget"), required_symbols)
        self.assertIn(("CLAUDE.md", "workBlockBootstrap.requireIntegratedStartHeadForFinalize"), required_symbols)
        self.assertIn(("closeout.config.json", "workBlockBootstrap"), required_symbols)
        self.assertIn(("closeout.config.json", "requireIntegratedStartHeadForFinalize"), required_symbols)
        self.assertIn(("closeout.config.json", "contentReviewGate"), required_symbols)
        self.assertIn(("closeout.config.json", "unifiedTruthReport"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def run_bounded_closeout_process"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def bounded_closeout_cli_main"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def auto_branch_from_protected_target"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def verify_repo_closed_postcondition"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def closeout_clean_truth_from_postcondition"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def stop_runtime_services_before_promotion"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def restart_runtime_services_after_clean_promotion"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def agent_remediation_queue_consumer_plan"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def collect_agent_remediation_results"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def write_evidence_preserving_prune_recovery"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def verify_prune_recovery_artifact"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def write_dirty_worktree_recovery_evidence"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "--require-repo-closed"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "agent-queue"), required_symbols)
        self.assertIn(("tools/repo_hygiene/work_block_cli.py", "agent-results"), required_symbols)
        self.assertIn(("tools/closeout/Invoke-CloseoutCli.ps1", "bounded_closeout_cli_main"), required_symbols)
        self.assertIn(("tools/closeout/Invoke-CloseoutCli.ps1", "closeout-heartbeat"), required_symbols)
        self.assertIn(("tools/closeout/Invoke-CloseoutCli.ps1", "CLOSEOUT_ADAPTER_HEARTBEAT_SECONDS"), required_symbols)
        self.assertIn(("tools/closeout/work-block-complete.ps1", "RequireRepoClosed"), required_symbols)
        self.assertIn(("tools/closeout/agent-remediation-queue.ps1", "agent-queue"), required_symbols)
        self.assertIn(("tools/closeout/agent-remediation-queue.ps1", "CollectResults"), required_symbols)

    def test_clean_integration_worktree_add_uses_longpaths_config(self) -> None:
        text = (ROOT / "tools" / "repo_hygiene" / "brokered_closeout.py").read_text(encoding="utf-8")
        self.assertIn("def run_git_longpaths", text)
        self.assertIn('"core.longpaths=true"', text)
        self.assertIn('run_git_longpaths(repo_root, ["worktree", "add", "--detach"', text)
        self.assertIn("add_worktree = run_git_longpaths(repo_root, add_args)", text)
        self.assertIn("add = run_git_longpaths(repo_root, add_args)", text)
        self.assertIn('run_git_longpaths(repo_root, ["worktree", "remove"', text)
        self.assertIn('run_git_longpaths(repo_root, ["worktree", "prune"', text)
        self.assertNotIn('run_git(repo_root, ["worktree", "add"', text)
        self.assertNotIn('run_git(repo_root, ["worktree", "remove"', text)

    def test_remove_worktree_prunes_stale_registry_after_path_is_already_gone(self) -> None:
        repo = self.init_repo()
        stale = self.tempdir / "stale-detached-worktree"
        git(repo, "worktree", "add", "--detach", str(stale), "HEAD")
        self.assertTrue(stale.exists())
        before = git(repo, "worktree", "list", "--porcelain").stdout
        self.assertIn(stale.name, before)
        shutil.rmtree(stale, ignore_errors=True)

        result = remove_worktree(repo, stale)

        self.assertEqual(result["pruneReturncode"], 0, result)
        after = git(repo, "worktree", "list", "--porcelain").stdout
        self.assertNotIn(stale.name, after)
        self.assertIn("worktree ", after)

    def test_bounded_runner_kills_hung_finalize_child_with_descendants(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        pid_path = repo / ".claude-state" / "closeout" / "descendant.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        child_code = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", child_code],
            timeout_ms=500,
            max_output_bytes=8192,
            recovery_command="rerun finalize",
            closeout_args=["finalize"],
        )

        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(result["returncode"], bounded_runner_exit_code(config, "timeout"), result)
        self.assertEqual(result["exitCodePolicy"], bounded_runner_exit_codes(config), result)
        self.assertTrue(result["killedProcessTree"], result)
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        for _ in range(30):
            if not process_is_running(descendant_pid):
                break
            time.sleep(0.1)
        self.assertFalse(process_is_running(descendant_pid), result)
        self.assertIn("bounded_runner_timeout", self.audit_types(repo))
        self.assertIn("bounded_runner_process_tree_killed", self.audit_types(repo))

    def test_bounded_runner_direct_finalize_and_completion_share_timeout_policy(self) -> None:
        repo = self.init_repo(config_updates={"locking": {"finalizeTimeoutMs": 1234}})
        config = load_closeout_config(repo)
        self.assertEqual(closeout_command_timeout_ms(config, ["finalize"]), 1234)
        self.assertEqual(closeout_command_timeout_ms(config, ["complete", "--finalize"]), 1234)

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_ms=200,
            max_output_bytes=8192,
            recovery_command="rerun work-block-complete -Finalize",
            closeout_args=["complete", "--finalize"],
        )

        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(result["returncode"], bounded_runner_exit_code(config, "timeout"), result)

    @mock.patch("tools.repo_hygiene.brokered_closeout.run_bounded_closeout_process")
    def test_bounded_closeout_run_uses_repo_local_module_path(self, run_process: mock.Mock) -> None:
        repo = self.init_repo()
        run_process.return_value = {"status": "ok"}

        result = bounded_closeout_run(repo, ["complete", "--finalize"])

        self.assertEqual(result["status"], "ok")
        run_process.assert_called_once()
        command = run_process.call_args.args[2]
        self.assertEqual(command[1], "-m")
        self.assertEqual(command[2], "tools.repo_hygiene.work_block_cli")
        self.assertIn("--repo-root", command)

    def test_bounded_runner_caps_oversized_child_output(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        code = "import sys, time; sys.stdout.write('x' * 131072); sys.stdout.flush(); time.sleep(60)"

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", code],
            timeout_ms=15000,
            max_output_bytes=4096,
            recovery_command="rerun capped child",
            closeout_args=["finalize"],
        )

        self.assertEqual(result["status"], "output_cap", result)
        self.assertEqual(result["returncode"], bounded_runner_exit_code(config, "output_cap"), result)
        self.assertGreater(result["stdoutBytes"], 4096)
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 4096)
        self.assertIn("bounded_runner_output_cap", self.audit_types(repo))

    def test_bounded_runner_exit_code_taxonomy_is_contract_required(self) -> None:
        config = load_closeout_config(ROOT)
        baseline = config["toolingBaseline"]
        required_symbols = {(item["path"], item["contains"]) for item in baseline["requiredSymbols"]}
        expected = {"timeout": 124, "outputCap": 125, "cpuStall": 126}

        self.assertEqual(config["locking"]["boundedRunnerExitCodes"], expected)
        self.assertEqual(bounded_runner_exit_codes(config), expected)
        self.assertEqual(bounded_runner_exit_code(config, "timeout"), 124)
        self.assertEqual(bounded_runner_exit_code(config, "output_cap"), 125)
        self.assertEqual(bounded_runner_exit_code(config, "cpu_stall"), 126)
        self.assertIn("test_bounded_runner_exit_code_taxonomy_is_contract_required", baseline["requiredTests"])
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def bounded_runner_exit_code"), required_symbols)
        self.assertIn(("closeout.config.json", "boundedRunnerExitCodes"), required_symbols)
        self.assertIn("timeout=124", (ROOT / "CLOSEOUT-STANDARD.md").read_text(encoding="utf-8"))
        self.assertIn("output cap=125", (ROOT / "docs" / "18-automatic-work-block-closeout-standard.md").read_text(encoding="utf-8"))

    def test_bounded_runner_normalizes_known_failure_text_with_zero_exit(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "print('review quorum failure')"],
            timeout_ms=15000,
            max_output_bytes=8192,
            recovery_command="rerun after quorum approval",
            closeout_args=["finalize"],
        )

        self.assertEqual(result["status"], "normalized_failure", result)
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("review quorum failure", result["matchedFailurePatterns"])
        self.assertIn("known_failure_text", result["normalizedReasons"])
        self.assertIn("bounded_runner_normalized_failure", self.audit_types(repo))

    def test_bounded_runner_trusts_finalize_semantic_success_over_validation_text(self) -> None:
        repo = self.init_repo(config_updates={"locking": {"failureTextPatterns": ["stale_refs", "validation_failed"]}})
        config = load_closeout_config(repo)
        audit_path = repo / ".claude-state" / "closeout" / "audits" / "audits.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps({"outcome": "success"}) + "\n", encoding="utf-8")
        payload = {
            "status": "success",
            "validations": [
                {
                    "returncode": 0,
                    "stdout": "test_stale_refs_block_finalize_before_mutation ... ok\nvalidation_failed vocabulary stayed in a passing test name",
                }
            ],
        }

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "import json; print(json.dumps(%r))" % payload],
            timeout_ms=5000,
            max_output_bytes=8192,
            recovery_command="rerun finalize",
            closeout_args=["finalize"],
        )

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["returncode"], 0, result)
        self.assertEqual(result["childStatus"], "success", result)
        self.assertIn("validation_failed", result["ignoredFailurePatterns"])
        self.assertEqual([], result["matchedFailurePatterns"])

    def test_bounded_runner_closeout_gate_failure_cannot_report_finalized(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        code = "print('closeout gate failure after finalize')"

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", code],
            timeout_ms=5000,
            max_output_bytes=8192,
            recovery_command="rerun closeout gate",
            closeout_args=["finalize"],
        )

        self.assertEqual(result["status"], "normalized_failure", result)
        self.assertIn("closeout gate failure", result["matchedFailurePatterns"])
        self.assertIn("known_failure_text", result["normalizedReasons"])

    def test_bounded_runner_review_quorum_failure_requires_approval_artifact_and_rerun(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        approval_path = repo / ".claude-state" / "closeout" / "reviews" / "approval.json"
        failing_code = "import json; print(json.dumps({'status': 'success', 'message': 'review quorum failure'}))"

        first = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", failing_code],
            timeout_ms=5000,
            max_output_bytes=8192,
            recovery_command="write approval and rerun validation",
            closeout_args=["review-quorum"],
            expected_success_artifact=approval_path,
        )
        self.assertEqual(first["status"], "normalized_failure", first)

        approval_path.parent.mkdir(parents=True, exist_ok=True)
        approval_path.write_text("{}", encoding="utf-8")
        second = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "import json; print(json.dumps({'status': 'success'}))"],
            timeout_ms=5000,
            max_output_bytes=8192,
            recovery_command="write approval and rerun validation",
            closeout_args=["review-quorum"],
            expected_success_artifact=approval_path,
        )
        self.assertEqual(second["status"], "success", second)

    def test_bounded_runner_timeout_leaves_no_orphan_child_processes(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_ms=200,
            max_output_bytes=8192,
            recovery_command="rerun timeout child",
            closeout_args=["repair"],
        )

        self.assertEqual(result["status"], "timeout", result)
        for _ in range(20):
            if not process_is_running(int(result["pid"])):
                break
            time.sleep(0.1)
        self.assertFalse(process_is_running(int(result["pid"])), result)

    def test_validation_commands_are_bounded_and_kill_descendants(self) -> None:
        repo = self.init_repo()
        pid_path = repo / ".claude-state" / "closeout" / "validation-descendant.pid"
        child_code = (
            "import pathlib, subprocess, sys, time\n"
            f"pid_path = pathlib.Path({str(pid_path)!r})\n"
            "pid_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "pid_path.write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        self.write_config(
            repo,
            {
                "validation": {
                    # Allow a loaded Windows host to start Python and its child;
                    # the test is about bounded termination, not startup speed.
                    "timeoutMs": 7500,
                    "maxOutputBytes": 8192,
                    "commands": [
                        {
                            "name": "hung-validation",
                            "argv": [sys.executable, "-c", child_code],
                            "pathPatterns": ["tools/repo_hygiene/**"],
                        }
                    ],
                }
            },
        )
        config = load_closeout_config(repo)

        results = run_validations(
            repo,
            config,
            repo,
            changed_paths=["tools/repo_hygiene/brokered_closeout.py"],
            work_block_id="wb-bounded-validation",
        )

        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["returncode"], 124, results)
        self.assertTrue(results[0]["timedOut"], results)
        self.assertTrue(results[0]["killedProcessTree"], results)
        self.assertTrue(pid_path.is_file(), results)
        descendant_pid = int(pid_path.read_text(encoding="utf-8"))
        for _ in range(30):
            if not process_is_running(descendant_pid):
                break
            time.sleep(0.1)
        self.assertFalse(process_is_running(descendant_pid), results)
        self.assertIn("bounded_runner_timeout", self.audit_types(repo))
        self.assertIn("bounded_runner_process_tree_killed", self.audit_types(repo))

    def test_validation_resource_policy_sets_below_normal_priority_and_affinity(self) -> None:
        repo = self.init_repo(
            config_updates={
                "processResources": {
                    "defaultPriority": "below_normal",
                    "validationPriority": "below_normal",
                    "validationAffinityCores": 2,
                    "cpuWatchdog": {
                        "enabled": True,
                        "thresholdPercent": 80,
                        "sustainedSeconds": 30,
                        "sampleIntervalSeconds": 2,
                        "requireNoOutputProgress": True,
                    },
                },
                "validation": {
                    "commands": [
                        {
                            "name": "resource-check",
                            "argv": [sys.executable, "-c", "print('ok')"],
                            "affinityCores": 1,
                            "cpuWatchdog": {"enabled": False},
                        }
                    ]
                },
            }
        )
        config = load_closeout_config(repo)

        policy = closeout_process_resource_policy(config, ["validation", "resource-check"], config["validation"]["commands"][0])
        self.assertEqual(policy["priority"], "below_normal")
        self.assertEqual(policy["affinityCores"], 1)
        self.assertFalse(policy["cpuWatchdog"]["enabled"])

        results = run_validations(repo, config, repo, changed_paths=["tools/repo_hygiene/brokered_closeout.py"])

        self.assertEqual(results[0]["returncode"], 0, results)
        self.assertEqual(results[0]["resourcePolicy"]["priority"], "below_normal")
        self.assertEqual(results[0]["resourcePolicy"]["affinityCores"], 1)
        self.assertIn("affinity", results[0]["resourceApply"])
        self.assertIn("treeAffinity", results[0]["resourceApply"])

    @unittest.skipUnless(os.name == "nt", "CPU watchdog currently samples Windows process trees")
    def test_bounded_runner_cpu_watchdog_terminates_hot_silent_child(self) -> None:
        repo = self.init_repo(
            config_updates={
                "processResources": {
                    "defaultPriority": "below_normal",
                    "cpuWatchdog": {
                        "enabled": True,
                        "thresholdPercent": 1,
                        "sustainedSeconds": 0.2,
                        "sampleIntervalSeconds": 0.1,
                        "requireNoOutputProgress": True,
                    },
                }
            }
        )
        config = load_closeout_config(repo)

        result = run_bounded_closeout_process(
            repo,
            config,
            [sys.executable, "-c", "while True:\n    pass\n"],
            timeout_ms=15000,
            max_output_bytes=8192,
            recovery_command="rerun cpu watchdog child",
            closeout_args=["repair"],
            work_block_id="wb-cpu-watchdog",
        )

        self.assertIn(result["status"], {"cpu_stall", "timeout"}, result)
        self.assertEqual(
            result["returncode"],
            bounded_runner_exit_code(config, "cpu_stall" if result["status"] == "cpu_stall" else "timeout"),
            result,
        )
        self.assertTrue(result["cpuStalled"] or result["timedOut"], result)
        if result["status"] == "cpu_stall":
            self.assertGreaterEqual(result["cpuWatchdog"]["lastCpuPercent"], 1, result)
        else:
            self.assertGreaterEqual(result["cpuWatchdog"]["lastCpuPercent"], 0, result)
        self.assertIn("bounded_runner_cpu_stall", self.audit_types(repo))
        self.assertIn("bounded_runner_process_tree_killed", self.audit_types(repo))

    def test_closeout_adapter_heartbeat_is_contract_required(self) -> None:
        config = load_closeout_config(ROOT)
        baseline = config["toolingBaseline"]
        script = (ROOT / "tools" / "closeout" / "Invoke-CloseoutCli.ps1").read_text(encoding="utf-8")
        required_symbols = {(item["path"], item["contains"]) for item in baseline["requiredSymbols"]}

        self.assertGreater(config["locking"]["adapterHeartbeatSeconds"], 0)
        self.assertLessEqual(config["locking"]["adapterHeartbeatSeconds"], 30)
        self.assertIn("test_closeout_adapter_heartbeat_is_contract_required", baseline["requiredTests"])
        self.assertIn(("tools/closeout/Invoke-CloseoutCli.ps1", "closeout-heartbeat"), required_symbols)
        self.assertIn(("tools/closeout/Invoke-CloseoutCli.ps1", "CLOSEOUT_ADAPTER_HEARTBEAT_SECONDS"), required_symbols)
        self.assertIn("RedirectStandardOutput", script)
        self.assertIn("RedirectStandardError", script)
        self.assertIn("GetTempPath", script)
        self.assertIn("WaitForExit", script)

    def test_full_validation_suite_is_skipped_without_explicit_request(self) -> None:
        env_name = "MLV_TEST_CLOSEOUT_RUN_FULL"
        repo = self.init_repo(
            config_updates={
                "validation": {
                    "fullSuiteEnvVar": env_name,
                    "runFullSuiteByDefault": False,
                    "commands": [
                        {"name": "smoke", "argv": [sys.executable, "-c", "print('smoke')"]},
                        {"name": "full", "runMode": "full", "argv": [sys.executable, "-c", "raise SystemExit(7)"]},
                    ],
                }
            }
        )
        config = load_closeout_config(repo)

        with mock.patch.dict(os.environ, {env_name: ""}, clear=False):
            results = run_validations(repo, config, repo, changed_paths=["tools/repo_hygiene/brokered_closeout.py"])

        self.assertFalse(validation_full_suite_requested(config, env={}))
        self.assertTrue(validation_full_suite_requested(config, env={env_name: "1"}))
        self.assertEqual(len(results), 2, results)
        self.assertEqual(results[0]["returncode"], 0, results)
        self.assertTrue(results[1]["skipped"], results)
        self.assertEqual(results[1]["skipReason"], "full_validation_not_requested")

    def test_path_scoped_validation_skips_unmatched_commands(self) -> None:
        repo = self.init_repo(
            config_updates={
                "validation": {
                    "commands": [
                        {
                            "name": "closeout-only",
                            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
                            "pathPatterns": ["tools/repo_hygiene/**"],
                        },
                        {
                            "name": "bridge-only",
                            "argv": [sys.executable, "-c", "print('bridge-ran')"],
                            "pathPatterns": ["tools/agent-bridge/**"],
                        },
                    ]
                }
            }
        )
        config = load_closeout_config(repo)

        results = run_validations(
            repo,
            config,
            repo,
            changed_paths=["tools/agent-bridge/server_wrapper.py"],
            work_block_id="wb-path-scoped-validation",
        )

        self.assertEqual(len(results), 2, results)
        self.assertTrue(results[0]["skipped"], results)
        self.assertEqual(results[0]["skipReason"], "path_patterns_not_matched")
        self.assertEqual(results[1]["returncode"], 0, results)
        self.assertIn("bridge-ran", results[1]["stdout"])

    def test_validation_commands_ignore_failure_words_in_successful_test_names(self) -> None:
        repo = self.init_repo(
            config_updates={
                "locking": {"failureTextPatterns": ["stale_refs", "validation_failed"]},
                "validation": {
                    "commands": [
                        {
                            "name": "successful-suite-with-blocker-vocabulary",
                            "argv": [
                                sys.executable,
                                "-c",
                                "print('test_stale_refs_block_finalize_before_mutation ... ok'); print('test_validation_failure_blocks_after_clean_merge ... ok')",
                            ],
                            "pathPatterns": ["tools/repo_hygiene/**"],
                        }
                    ],
                },
            }
        )
        config = load_closeout_config(repo)

        results = run_validations(
            repo,
            config,
            repo,
            changed_paths=["tools/repo_hygiene/brokered_closeout.py"],
            work_block_id="wb-validation-vocabulary",
        )

        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["returncode"], 0, results)
        self.assertEqual(results[0]["status"], "success", results)
        self.assertFalse(results[0]["timedOut"], results)

    def test_timeout_and_output_cap_settings_are_contract_required(self) -> None:
        contract = broker_contract(ROOT)
        config = load_closeout_config(ROOT)
        baseline = config["toolingBaseline"]
        self.assertIn("locking", contract["requiredConfigKeys"])
        self.assertIn("autoEligibilityRepair", contract["requiredConfigKeys"])
        self.assertIn("processResources", contract["requiredConfigKeys"])
        self.assertIn("powerShell", contract["requiredConfigKeys"])
        self.assertIn("powerShell.preferredExecutable", contract["requiredConfigKeys"])
        self.assertIn("powerShell.requiredArgs", contract["requiredConfigKeys"])
        self.assertIn("locking", baseline["requiredConfigKeys"])
        self.assertIn("autoEligibilityRepair", baseline["requiredConfigKeys"])
        self.assertIn("processResources", baseline["requiredConfigKeys"])
        self.assertIn("powerShell", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.preferredExecutable", baseline["requiredConfigKeys"])
        self.assertIn("powerShell.requiredArgs", baseline["requiredConfigKeys"])
        self.assertGreater(closeout_command_timeout_ms(config, ["detect"]), 0)
        self.assertGreater(closeout_command_timeout_ms(config, ["repair"]), 0)
        self.assertGreater(closeout_command_timeout_ms(config, ["finalize"]), 0)
        self.assertGreaterEqual(
            closeout_max_process_output_bytes(config),
            8 * 1024 * 1024,
            "sweep and retained-remediation reports can exceed 1 MiB in established workspaces",
        )
        self.assertGreater(config["validation"]["timeoutMs"], 0)
        self.assertGreaterEqual(
            config["validation"]["maxOutputBytes"],
            8 * 1024 * 1024,
            "repo-sweep candidate reports can exceed 1 MiB in established workspaces",
        )
        self.assertIn("codex/f1-visible-scope-policy", config["git"]["protectedBranches"])
        self.assertIn("closeout/recovery/detached/*", config["git"]["protectedBranches"])
        self.assertTrue(
            {
                "build/provenance-p0",
                "claude/export-hardening",
                "claude/sleepy-brahmagupta-3996c3",
                "codex/cuda-chroma-x1",
                "codex/laon-sh-no-readback",
                "codex/optimus-m2-x1",
                "codex/ui-signal-gap-instrument",
                "codex/w10-agent-heartbeat-hardening",
                "codex/w10c-agent-reliability",
            }.issubset(config["git"]["protectedBranches"]),
            "the enumerated CI-1 legacy branches must remain parked instead of being merged or pruned",
        )
        self.assertIn(
            "**/.claude-state/worktrees/f1-visible-scope-policy",
            config["cleanupPolicy"]["protectedWorktreeRoots"],
        )
        self.assertIn(
            "**/.claude-state/closeout/repo-sweep/detached-preserve/**",
            config["cleanupPolicy"]["protectedWorktreeRoots"],
        )
        self.assertTrue(
            {
                "C:/!Layi Wkspc/MLV-App",
                "C:/!Layi Wkspc/MLV-App/.claude/worktrees/sleepy-brahmagupta-3996c3",
                "C:/mlvtmp/mlvapp-export-hardening-claude",
                "C:/mlvtmp/codex-w10-agent-heartbeat-hardening",
                "C:/mlvtmp/codex-w10c-agent-reliability",
                "C:/mlvtmp/mlvapp-5aa7cebe-codex-ui-signal",
                "C:/mlvtmp/mlvapp-f28dbca7-cuda-chroma-codex",
                "C:/mlvtmp/mlvapp-f28dbca7-laon-nr-codex",
                "C:/mlvtmp/mlvapp-optimus-m2-codex",
                "C:/mlvtmp/opus-review-bugA",
                "C:/mlvtmp/wt-baseline",
                "C:/mlvtmp/wt-bisect",
                "C:/mlvtmp/wt-good-765ed4a3",
                "C:/mlvtmp/wt-precuda",
            }.issubset(config["cleanupPolicy"]["protectedWorktreeRoots"]),
            "the enumerated CI-1 legacy worktrees must remain byte-preserved and owner-controlled",
        )
        validation_names = [command["name"] for command in config["validation"]["commands"]]
        self.assertEqual(
            validation_names[:3],
            [
                "brokered-closeout-smoke-core",
                "brokered-closeout-smoke-state",
                "brokered-closeout-smoke-dashboard",
            ],
        )
        self.assertNotIn("brokered-closeout-smoke", validation_names)
        self.assertIn("pathPatterns", config["validation"]["commands"][0])
        self.assertEqual(config["processResources"]["defaultPriority"], "below_normal")
        self.assertEqual(config["processResources"]["validationPriority"], "below_normal")
        self.assertEqual(config["processResources"]["validationAffinityCores"], 2)
        self.assertTrue(config["processResources"]["cpuWatchdog"]["enabled"])
        self.assertGreater(config["locking"]["adapterHeartbeatSeconds"], 0)
        self.assertLessEqual(config["locking"]["adapterHeartbeatSeconds"], 30)
        self.assertEqual(config["locking"]["boundedRunnerExitCodes"], {"timeout": 124, "outputCap": 125, "cpuStall": 126})
        self.assertIn("test_validation_commands_are_bounded_and_kill_descendants", baseline["requiredTests"])
        self.assertIn("test_validation_resource_policy_sets_below_normal_priority_and_affinity", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_cpu_watchdog_terminates_hot_silent_child", baseline["requiredTests"])
        self.assertIn("test_closeout_adapter_heartbeat_is_contract_required", baseline["requiredTests"])
        self.assertIn("test_full_validation_suite_is_skipped_without_explicit_request", baseline["requiredTests"])
        self.assertIn("test_path_scoped_validation_skips_unmatched_commands", baseline["requiredTests"])
        self.assertIn("test_validation_commands_ignore_failure_words_in_successful_test_names", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_caps_oversized_child_output", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_exit_code_taxonomy_is_contract_required", baseline["requiredTests"])
        self.assertIn("test_bounded_runner_trusts_finalize_semantic_success_over_validation_text", baseline["requiredTests"])
        self.assertLessEqual(config["validation"]["timeoutMs"], 120000)
        full_commands = [command for command in config["validation"]["commands"] if command.get("runMode") == "full"]
        self.assertTrue(full_commands)
        self.assertGreaterEqual(full_commands[0]["timeoutMs"], 600000)

    def test_stale_tooling_without_bounded_runner_reports_tooling_drift(self) -> None:
        repo = self.init_repo(
            config_updates={
                "toolingBaseline": {
                    "enabled": True,
                    "autoUpdate": False,
                    "requiredTests": [],
                    "requiredSymbols": [
                        {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def run_bounded_closeout_process"}
                    ],
                }
            }
        )
        self.make_feature(repo, "wb-bounded-runner-drift")
        (repo / "work.txt").write_text("dirty blocker should not hide stale runner\n", encoding="utf-8")

        result = finalize_work_block(repo, work_block_id="wb-bounded-runner-drift")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "closeout_tooling_stale")
        self.assertIn("symbol", {item["kind"] for item in result["tooling"]["missing"]})
        self.assertIn("closeout_tooling_stale", self.audit_types(repo))

    def test_hard_clean_final_response_blocks_non_exempt_dirty_files(self) -> None:
        repo = self.init_repo()
        (repo / "scratch.txt").write_text("not closed\n", encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked")
        self.assertIn("non_exempt_dirty_files", {item["kind"] for item in result["blockers"]})
        self.assertIn("non_exempt_untracked_files", {item["kind"] for item in result["blockers"]})
        self.assertIn("repo_closed_postcondition", self.audit_types(repo))

    def test_hard_clean_exempts_generated_codex_state_agent_loop_prompts(self) -> None:
        repo = self.init_repo()
        prompt = repo / ".codex-state" / "agent-loop" / "inbox" / "loop-test--to-codex.prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("generated prompt state\n", encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["blockers"], result)

    def test_hard_clean_final_response_blocks_remaining_stash(self) -> None:
        repo = self.init_repo()
        (repo / "README.md").write_text("stashed\n", encoding="utf-8")
        git(repo, "stash", "push", "-m", "leftover work")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked")
        self.assertIn("disallowed_stashes", {item["kind"] for item in result["blockers"]})

    def test_hard_clean_final_response_passes_after_clean_promotion(self) -> None:
        repo = self.init_repo()

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["blockers"])

    def test_repo_closed_postcondition_does_not_treat_primary_root_as_stale_sibling(self) -> None:
        repo = self.init_repo()
        git(repo, "branch", "codex/side-inspection")
        side = self.tempdir / "side-inspection"
        git(repo, "worktree", "add", str(side), "codex/side-inspection")
        git(repo, "checkout", "--detach")
        (repo / "README.md").write_text("foreign dirty primary root\n", encoding="utf-8")

        result = verify_repo_closed_postcondition(side, work_block_id=None, finalize_result={"status": "success"})

        stale = result["worktreeState"]["staleManagedWorktrees"]
        self.assertNotIn(repo.resolve(), {Path(item["path"]).resolve() for item in stale})
        self.assertNotIn("stale_managed_worktrees", {item["kind"] for item in result["blockers"]})

    def test_repo_closed_postcondition_still_blocks_genuine_stale_detached_sibling(self) -> None:
        repo = self.init_repo()
        git(repo, "branch", "codex/side-inspection")
        side = self.tempdir / "side-inspection"
        stale_sibling = self.tempdir / "stale-detached-sibling"
        git(repo, "worktree", "add", str(side), "codex/side-inspection")
        git(repo, "worktree", "add", "--detach", str(stale_sibling), "HEAD")

        result = verify_repo_closed_postcondition(side, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        stale = result["worktreeState"]["staleManagedWorktrees"]
        self.assertIn(stale_sibling.resolve(), {Path(item["path"]).resolve() for item in stale})
        self.assertIn("stale_managed_worktrees", {item["kind"] for item in result["blockers"]})

    def test_repo_closed_postcondition_blocks_linked_sibling_worktree(self) -> None:
        repo = self.init_repo()
        sibling = self.tempdir / "ordinary-sibling-worktree"
        git(repo, "branch", "codex/ordinary-sibling")
        git(repo, "worktree", "add", str(sibling), "codex/ordinary-sibling")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("linked_sibling_worktrees", blocker_kinds)
        inspection = result["worktreeState"]["inspection"]
        self.assertTrue(inspection["currentRootPresent"])
        self.assertEqual(inspection["ordinaryLinkedWorktreeCount"], 1)
        self.assertEqual(Path(inspection["ordinaryLinkedWorktrees"][0]["path"]).resolve(), sibling.resolve())
        truth = result["closeoutCleanTruth"]
        self.assertFalse(truth["cleanup"]["clean"])
        self.assertFalse(truth["cleanup"]["linkedSiblingWorktreeClean"])

    def test_repo_closed_postcondition_reports_unified_closeout_clean_truth(self) -> None:
        repo = self.init_repo()

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        truth = result["closeoutCleanTruth"]
        self.assertEqual(truth, closeout_clean_truth_from_postcondition(result))
        self.assertEqual(truth["artifactKind"], "closeoutCleanTruth")
        self.assertEqual(truth["authoritativeSource"], "repoClosedPostcondition")
        self.assertEqual(truth["status"], "clean")
        self.assertTrue(truth["repoClosed"])
        self.assertTrue(truth["rawGit"]["clean"])
        self.assertTrue(truth["policy"]["clean"])
        self.assertTrue(truth["cleanup"]["clean"])

    def test_closeout_clean_truth_preserves_raw_git_dirty_but_policy_clean_for_exempt_state(self) -> None:
        repo = self.init_repo()
        prompt = repo / ".codex-state" / "agent-loop" / "inbox" / "loop-test--to-codex.prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("generated prompt state\n", encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        truth = result["closeoutCleanTruth"]
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(truth["status"], "clean")
        self.assertFalse(truth["rawGit"]["clean"])
        self.assertEqual(truth["rawGit"]["statusEntryCount"], 1)
        self.assertTrue(truth["policy"]["clean"])
        self.assertTrue(truth["cleanup"]["clean"])

    def test_closeout_clean_truth_contract_required(self) -> None:
        config = load_closeout_config(ROOT)
        self.assertTrue(config["hardClean"]["unifiedTruthReport"]["enabled"])
        self.assertEqual(config["hardClean"]["unifiedTruthReport"]["authoritativeSource"], "repoClosedPostcondition.closeoutCleanTruth")
        baseline = config["toolingBaseline"]
        self.assertIn("test_repo_closed_postcondition_reports_unified_closeout_clean_truth", baseline["requiredTests"])
        self.assertIn("test_repo_closed_postcondition_blocks_linked_sibling_worktree", baseline["requiredTests"])
        self.assertIn("test_closeout_clean_truth_preserves_raw_git_dirty_but_policy_clean_for_exempt_state", baseline["requiredTests"])
        self.assertIn("test_closeout_clean_truth_contract_required", baseline["requiredTests"])
        required_symbols = {(item["path"], item["contains"]) for item in baseline["requiredSymbols"]}
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def closeout_clean_truth_from_postcondition"), required_symbols)
        self.assertIn(("tools/repo_hygiene/brokered_closeout.py", "def worktree_inspection_state"), required_symbols)
        self.assertIn(("closeout.config.json", "unifiedTruthReport"), required_symbols)
        self.assertIn(("AGENTS.md", "closeoutCleanTruth"), required_symbols)
        self.assertIn(("CLAUDE.md", "closeoutCleanTruth"), required_symbols)

    def test_complete_finalize_enforces_hard_clean_config_without_switch(self) -> None:
        repo = self.init_repo()
        self.make_feature(repo, "wb-hard-clean-default")
        (repo / "README.md").write_text("stash keeps repo open\n", encoding="utf-8")
        git(repo, "stash", "push", "-m", "hard clean blocker")

        result = complete_work_block(repo, work_block_id="wb-hard-clean-default", finalize=True)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "repo_closed_postcondition_failed")
        self.assertIn("repoClosedPostcondition", result)
        blocker_kinds = {item["kind"] for item in result["repoClosedPostcondition"]["blockers"]}
        self.assertIn("disallowed_stashes", blocker_kinds)

    def test_work_block_complete_wrapper_finalizes_by_default(self) -> None:
        script_text = (ROOT / "tools" / "closeout" / "work-block-complete.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Finalize", script_text)
        self.assertIn("[switch]$NoFinalize", script_text)
        self.assertIn("if ($Finalize -or -not $NoFinalize)", script_text)
        self.assertIn('$argsList += "--finalize"', script_text)

    def test_complete_finalize_on_clean_protected_target_is_noop_repo_closed(self) -> None:
        repo = self.init_repo(remote=True)

        result = complete_work_block(repo, finalize=True)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["reason"], "protected_target_repo_closed")
        self.assertEqual(result["finalizeStatus"], "noop")
        self.assertIsNone(result["selectedWorkBlockId"])
        self.assertEqual(result["workBlockSelection"]["selectionReason"], "protected_target_no_active_work_block")
        self.assertTrue(result["repoClosedPostcondition"]["ok"])
        self.assertEqual(list((repo / ".claude-state" / "closeout" / "work-blocks").glob("*/manifest.json")), [])
        self.assertIn("protected-target-noop-closeout", self.audit_types(repo))

    def test_dirty_protected_target_finalize_recovers_to_feature_branch(self) -> None:
        repo = self.init_repo(remote=True)
        (repo / "dirty.txt").write_text("target dirty\n", encoding="utf-8")

        result = complete_work_block(repo, finalize=True)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "protected_target_dirty_recovered_to_feature_branch")
        self.assertEqual(result["finalizeStatus"], "recovered-to-feature-branch")
        blocker_kinds = {item["kind"] for item in result["repoClosedPostcondition"]["blockers"]}
        self.assertIn("non_exempt_dirty_files", blocker_kinds)
        branch = git(repo, "branch", "--show-current").stdout.strip()
        self.assertTrue(branch.startswith("codex/work-block/"), branch)
        manifests = list((repo / ".claude-state" / "closeout" / "work-blocks").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["branch"], branch)
        self.assertEqual(manifest["protectedBranchBootstrap"]["fromProtectedBranch"], "master")
        self.assertEqual(manifest["protectedBranchBootstrap"]["reason"], "protected_branch_dirty_recovery")
        self.assertIn("dirty.txt", manifest["dirtyBaseline"]["paths"])
        self.assertEqual(manifest["pathClaims"], ["dirty.txt"])
        detection = detect_work_block(repo, work_block_id=manifest["workBlockId"])
        self.assertEqual([item["path"] for item in detection["ownedDirty"]], ["dirty.txt"])
        self.assertEqual(detection["foreignDirty"], [])
        finalize_result = finalize_work_block(repo, work_block_id=manifest["workBlockId"])
        self.assertEqual(finalize_result["status"], "success", finalize_result)
        self.assertEqual(git(repo, "show", "master:dirty.txt").stdout, "target dirty\n")
        self.assertIn("protected-target-dirty-recovery", self.audit_types(repo))

    def test_dirty_protected_target_recovery_owns_paths_despite_stale_claim(self) -> None:
        repo = self.init_repo(remote=True)
        (repo / "dirty.txt").write_text("target dirty\n", encoding="utf-8")

        result = complete_work_block(repo, finalize=True)
        manifest = result["recoveryManifest"]

        with mock.patch(
            "tools.repo_hygiene.brokered_closeout.active_path_claims",
            return_value={"dirty.txt": "wb-stale-claim"},
        ):
            detection = detect_work_block(repo, work_block_id=manifest["workBlockId"])

        self.assertEqual([item["path"] for item in detection["ownedDirty"]], ["dirty.txt"])
        self.assertEqual(detection["ownedDirty"][0]["classificationReason"], "protected target dirty recovery ownership proof")
        self.assertEqual(detection["foreignDirty"], [])

    def test_hard_clean_blocks_retained_remote_feature_refs(self) -> None:
        repo = self.init_repo(remote=True)
        git(repo, "checkout", "-b", "codex/remote-left-open")
        (repo / "left-open.txt").write_text("remote open\n", encoding="utf-8")
        git(repo, "add", "left-open.txt")
        git(repo, "commit", "-m", "remote left open")
        git(repo, "push", "origin", "codex/remote-left-open")
        git(repo, "checkout", "master")
        git(repo, "branch", "-D", "codex/remote-left-open")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("retained_remote_feature_refs", blocker_kinds)

    def test_review_quorum_requires_allowed_ten_score_self_plus_two_independent(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        pinned_refs = {"target": {"branch": "master", "head": git(repo, "rev-parse", "HEAD").stdout.strip()}}
        candidate_id = "candidate:quorum-shape"
        action_id = "clean_integrate"
        evidence_hash = "evidence-shape"
        record_review_approval(
            repo,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
            reviewer="local-test",
            approved=True,
            details={"score": 10},
        )
        record_review_approval(
            repo,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
            reviewer="codex-self",
            approved=True,
            details={"score": 9},
        )
        record_review_approval(
            repo,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
            reviewer="ancestry-safety-reviewer",
            approved=True,
            details={"score": 10},
        )
        record_review_approval(
            repo,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
            reviewer="mutation-scope-reviewer",
            approved=True,
            details={"score": 10},
        )

        low_score = check_review_quorum(
            repo,
            config,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
        )
        self.assertFalse(low_score["ok"])
        self.assertEqual(low_score["selfApprovalCount"], 0)
        self.assertEqual(low_score["lowScoreApprovalCount"], 1)

        record_review_approval(
            repo,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
            reviewer="codex-self",
            approved=True,
            details={"score": 10},
        )
        quorum = check_review_quorum(
            repo,
            config,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
        )
        self.assertTrue(quorum["ok"], quorum)
        self.assertGreaterEqual(quorum["matchingApprovals"], 3)
        self.assertEqual(quorum["selfApprovalCount"], 1)
        self.assertEqual(quorum["independentApprovalCount"], 2)

    def test_declared_review_surfaces_write_unavailable_reports(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        surfaces = config["reviewQuorum"]["declaredSurfaces"]
        pinned_refs = {"target": {"branch": "master", "head": git(repo, "rev-parse", "HEAD").stdout.strip()}}
        candidate_id = "candidate:review-surface"
        action_id = "clean_integrate"
        evidence_hash = "evidence-review-surface"

        reports = [
            write_review_surface_unavailable_report(
                repo,
                surface=surface["surface"],
                candidate_id=candidate_id,
                action_id=action_id,
                evidence_hash=evidence_hash,
                pinned_refs=pinned_refs,
                unavailable_reason="surface unavailable in focused test",
            )
            for surface in surfaces
        ]
        quorum = check_review_quorum(
            repo,
            config,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=pinned_refs,
        )

        self.assertEqual({item["surface"] for item in reports}, {item["surface"] for item in surfaces})
        self.assertEqual({item["status"] for item in reports}, {"review_surface_unavailable"})
        self.assertEqual(quorum["insufficientReviewStatus"], "insufficient_review_quorum")
        self.assertEqual(set(quorum["declaredReviewSurfaces"]), {item["surface"] for item in surfaces})
        for report in reports:
            path = repo / report["reportPath"]
            self.assertTrue(path.exists(), report)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["declaredReviewSurface"])
            self.assertEqual(payload["tupleHash"], report["tupleHash"])
            self.assertEqual(payload["status"], "review_surface_unavailable")
            self.assertTrue(payload["recoveryCommand"])
        self.assertIn("review_surface_unavailable", self.audit_types(repo))

    def test_runtime_service_stops_before_validation_and_restarts_after_repo_closed(self) -> None:
        marker = self.tempdir / "runtime-running.marker"
        log = self.tempdir / "runtime.log"
        marker.write_text("running\n", encoding="utf-8")
        repo = self.init_repo(config_updates=self.runtime_service_updates(marker, log))
        self.make_feature(repo, "wb-runtime-service")
        self.approve_current_tuple(repo, "wb-runtime-service")

        result = finalize_work_block(repo, work_block_id="wb-runtime-service")

        self.assertEqual(result["status"], "success", result)
        self.assertTrue(marker.exists(), result)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["stop", "validate:stopped", "start"])
        self.assertEqual(result["runtimeLifecycle"]["stopBeforePromotion"]["status"], "success")
        self.assertEqual(result["runtimeLifecycle"]["restartAfterCleanPromotion"]["status"], "success")

    def test_runtime_service_not_restarted_after_failed_validation_stale_refs_or_repo_closed_failure(self) -> None:
        marker = self.tempdir / "runtime-running-fail.marker"
        log = self.tempdir / "runtime-fail.log"
        marker.write_text("running\n", encoding="utf-8")
        repo = self.init_repo(config_updates=self.runtime_service_updates(marker, log, validation_rc=9))
        self.make_feature(repo, "wb-runtime-service-validation-failure")
        self.approve_current_tuple(repo, "wb-runtime-service-validation-failure")

        result = finalize_work_block(repo, work_block_id="wb-runtime-service-validation-failure", require_repo_closed=True)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "validation_failed")
        self.assertFalse(marker.exists(), result)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["stop", "validate:stopped"])
        self.assertIsNone(result["runtimeLifecycle"]["restartAfterCleanPromotion"])

    def test_closeout_tooling_stale_reports_missing_hard_clean_gate(self) -> None:
        repo = self.init_repo(
            config_updates={
                "toolingBaseline": {
                    "enabled": True,
                    "autoUpdate": False,
                    "requiredConfigKeys": ["hardClean"],
                    "requiredTests": [],
                    "requiredSymbols": [],
                }
            }
        )
        config_path = repo / "closeout.config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("hardClean", None)
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        git(repo, "add", "closeout.config.json")
        git(repo, "commit", "-m", "remove hard clean gate")
        self.make_feature(repo, "wb-missing-hard-clean")

        result = finalize_work_block(repo, work_block_id="wb-missing-hard-clean")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "closeout_tooling_stale")
        self.assertIn({"kind": "config_key", "key": "hardClean"}, result["tooling"]["missing"])

    def test_closeout_tooling_stale_reports_missing_power_shell_policy(self) -> None:
        repo = self.init_repo(
            config_updates={
                "powerShell": {"preferredExecutable": "pwsh.exe"},
                "toolingBaseline": {
                    "enabled": True,
                    "autoUpdate": False,
                    "requiredConfigKeys": [
                        "powerShell.preferredExecutable",
                        "powerShell.requiredArgs",
                        "powerShell.windowsPowerShellOnly",
                        "powerShell.fallbackOnlyWhenPwshUnavailable",
                    ],
                    "requiredTests": [],
                    "requiredSymbols": [],
                }
            }
        )
        self.make_feature(repo, "wb-missing-powershell-policy")

        result = finalize_work_block(repo, work_block_id="wb-missing-powershell-policy")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "closeout_tooling_stale")
        self.assertIn({"kind": "config_key", "key": "powerShell.requiredArgs"}, result["tooling"]["missing"])
        self.assertIn({"kind": "config_key", "key": "powerShell.windowsPowerShellOnly"}, result["tooling"]["missing"])
        self.assertIn({"kind": "config_key", "key": "powerShell.fallbackOnlyWhenPwshUnavailable"}, result["tooling"]["missing"])

    def test_finalize_loop_stops_on_repeated_identical_blocker_evidence_tuple(self) -> None:
        config = load_closeout_config(ROOT)
        pinned_refs = {"feature": {"head": "a"}, "target": {"head": "b"}}
        first = finalize_retry_decision(
            config,
            blocker_kind="target_push_rerun_required",
            evidence_hash_before="evidence-a",
            evidence_hash_after="evidence-b",
            pinned_refs_before_retry=pinned_refs,
            pins_match=True,
            retry_number=0,
            seen_tuples=[],
        )
        self.assertTrue(first["shouldRetry"], first)
        second = finalize_retry_decision(
            config,
            blocker_kind="target_push_rerun_required",
            evidence_hash_before="evidence-a",
            evidence_hash_after="evidence-c",
            pinned_refs_before_retry=pinned_refs,
            pins_match=True,
            retry_number=1,
            seen_tuples=[first["blockerEvidenceTuple"]],
        )
        self.assertFalse(second["shouldRetry"], second)
        self.assertEqual(second["terminalReason"], "repeated_identical_blocker_evidence_tuple")

    def test_finalize_loop_continues_when_evidence_repair_changes_tuple_and_pins_match(self) -> None:
        config = load_closeout_config(ROOT)
        decision = finalize_retry_decision(
            config,
            blocker_kind="final_push_evidence_repaired",
            evidence_hash_before="evidence-before",
            evidence_hash_after="evidence-after",
            pinned_refs_before_retry={"feature": {"head": "same"}, "target": {"head": "same"}},
            pins_match=True,
            retry_number=0,
            seen_tuples=[],
        )
        self.assertTrue(decision["shouldRetry"], decision)
        self.assertEqual(decision["symbolicRepairAttempted"], "evidence_repair")
        self.assertIsNone(decision["terminalReason"])

    def test_finalize_loop_renews_stale_review_as_safe_second_order_repair(self) -> None:
        config = load_closeout_config(ROOT)
        decision = finalize_retry_decision(
            config,
            blocker_kind="stale_review",
            evidence_hash_before="evidence-before",
            evidence_hash_after="evidence-after",
            pinned_refs_before_retry={"feature": {"head": "same"}, "target": {"head": "same"}},
            pins_match=True,
            retry_number=0,
            seen_tuples=[],
        )
        self.assertTrue(decision["shouldRetry"], decision)
        self.assertEqual(decision["symbolicRepairAttempted"], "renew_stale_review")
        self.assertIsNone(decision["terminalReason"])

    def test_finalize_loop_reruns_validation_failed_as_safe_second_order_repair(self) -> None:
        config = load_closeout_config(ROOT)
        decision = finalize_retry_decision(
            config,
            blocker_kind="validation_failed",
            evidence_hash_before="evidence-before",
            evidence_hash_after="evidence-after",
            pinned_refs_before_retry={"feature": {"head": "same"}, "target": {"head": "same"}},
            pins_match=True,
            retry_number=0,
            seen_tuples=[],
        )
        self.assertTrue(decision["shouldRetry"], decision)
        self.assertEqual(decision["symbolicRepairAttempted"], "rerun_validation_smoke")
        self.assertIsNone(decision["terminalReason"])

    def test_evidence_repair_commit_message_falls_back_to_generated_detail(self) -> None:
        config = load_closeout_config(ROOT)
        message = evidence_repair_commit_message(
            config,
            reason="final_push",
            work_block_id="wb-demo",
            paths=[
                ".closeout-evidence/wb-demo/closeout.json",
                ".closeout-evidence/wb-demo/metrics.json",
                ".closeout-evidence/wb-demo/session.json",
            ],
        )
        self.assertEqual(
            message,
            "chore(closeout): repair closeout.json, metrics.json, and session.json for wb-demo before final push",
        )

    def test_evidence_repair_commit_message_uses_work_block_subject(self) -> None:
        config = load_closeout_config(ROOT)
        message = evidence_repair_commit_message(
            config,
            reason="final_push",
            work_block_id="wb-demo",
            paths=[
                ".closeout-evidence/wb-demo/closeout.json",
                ".closeout-evidence/wb-demo/metrics.json",
                ".closeout-evidence/wb-demo/session.json",
            ],
            work_summary="export: apply Auto Look Assist raw defaults",
        )
        self.assertEqual(
            message,
            "evidence: export: apply Auto Look Assist raw defaults\n\n"
            "Closeout: repair closeout.json, metrics.json, and session.json before final push.\n"
            "Work block: wb-demo.",
        )

    def test_evidence_repair_commit_message_marks_generated_commit(self) -> None:
        config = load_closeout_config(ROOT)
        message = evidence_repair_commit_message(
            config,
            reason="final_push",
            work_block_id="wb-demo",
            paths=[".closeout-evidence/wb-demo/closeout.json"],
            work_summary="closeout: complete gate follow-up",
        )
        self.assertTrue(message.startswith("evidence: closeout: complete gate follow-up\n\n"))

    def test_evidence_repair_uses_delivered_subject_not_aspirational_work_block(self) -> None:
        repo = self.init_repo(config_updates={"evidenceRepair": {"enabled": True}})
        start_work_block(
            repo,
            work_block_id="wb-delivered-subject",
            actor="local-test",
            summary="P-pre: implement GPU AMaZE debayer parity",
        )
        (repo / "tools").mkdir()
        (repo / "tools" / "stage-probe.txt").write_text("stage probe only\n", encoding="utf-8")
        git(repo, "add", "tools/stage-probe.txt")
        git(repo, "commit", "-m", "P-pre: add CUDA AMaZE debayer stage probe")

        config = load_closeout_config(repo)
        detection = detect_work_block(repo, work_block_id="wb-delivered-subject")
        manifest = load_manifest(repo, config, "wb-delivered-subject")

        self.assertEqual(
            delivered_work_commit_subject(repo, detection, manifest),
            "P-pre: add CUDA AMaZE debayer stage probe",
        )

        result = repair_missing_evidence(repo, config, detection, reason="final_push")

        self.assertEqual(result["status"], "success")
        self.assertEqual(git(repo, "log", "-1", "--format=%s").stdout.strip(), "evidence: P-pre: add CUDA AMaZE debayer stage probe")
        self.assertIn("Closeout: repair", git(repo, "log", "-1", "--format=%B").stdout)

        post_evidence_detection = detect_work_block(repo, work_block_id="wb-delivered-subject")
        self.assertEqual(
            delivered_work_commit_subject(repo, post_evidence_detection, manifest),
            "P-pre: add CUDA AMaZE debayer stage probe",
        )

    def test_checkpoint_commit_message_falls_back_to_generated_detail(self) -> None:
        message = checkpoint_commit_message("wb-demo", ["tools/repo_hygiene/brokered_closeout.py", "closeout.config.json"])
        self.assertEqual(
            message,
            "chore(closeout): checkpoint brokered_closeout.py, and closeout.config.json for wb-demo",
        )

    def test_checkpoint_commit_message_appends_work_block_detail_to_subject(self) -> None:
        message = checkpoint_commit_message(
            "wb-demo",
            ["tools/repo_hygiene/brokered_closeout.py", "closeout.config.json"],
            explicit_message="brokered closeout checkpoint",
            work_summary="closeout: require human commit subjects",
        )
        self.assertEqual(
            message,
            "closeout: require human commit subjects\n\n"
            "Closeout: checkpoint brokered_closeout.py, and closeout.config.json.\n"
            "Work block: wb-demo.",
        )

    def test_dirty_split_commit_message_is_human_readable(self) -> None:
        message = dirty_split_commit_message("wb-demo", ["closeout.config.json", "docs/19-closeout-dashboard-spec.md"])
        self.assertEqual(
            message,
            "chore(closeout): preserve split-owned changes for wb-demo (closeout.config.json, and 19-closeout-dashboard-spec.md)",
        )

    def test_closeout_merge_commit_message_falls_back_to_generated_detail(self) -> None:
        work_block_merge = closeout_merge_commit_message("codex/work-block/wb-demo", "master")
        split_merge = closeout_merge_commit_message("closeout/split/wb-demo", "master")
        self.assertEqual(work_block_merge, "merge(closeout): integrate wb-demo closeout hardening into master")
        self.assertEqual(split_merge, "merge(closeout): integrate preserved split changes from wb-demo into master")

    def test_closeout_merge_commit_message_appends_work_block_detail_to_subject(self) -> None:
        message = closeout_merge_commit_message(
            "codex/work-block/wb-demo",
            "master",
            work_summary="closeout: require human commit subjects",
        )
        self.assertEqual(
            message,
            "integration: closeout: require human commit subjects\n\n"
            "Closeout: integrate work block wb-demo into master after clean validation.",
        )

    def test_completion_without_explicit_work_block_id_reports_deterministic_selection_reason(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/select-latest")
        old = start_work_block(repo, work_block_id="wb-a-old", actor="local-test")
        old_manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / old["workBlockId"] / "manifest.json"
        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
        old_manifest.update(
            {
                "state": "blocked",
                "blockedReason": "review_quorum_missing",
                "updatedAt": "2026-01-01T00:00:00+00:00",
            }
        )
        old_manifest_path.write_text(json.dumps(old_manifest, indent=2), encoding="utf-8")
        start_work_block(repo, work_block_id="wb-z-new", actor="local-test")

        result = complete_work_block(repo, finalize=False)

        self.assertEqual(result["workBlockId"], "wb-z-new")
        self.assertEqual(result["workBlockSelection"]["reason"], "selected_by_branch_state_updated_workBlockId")
        self.assertEqual(result["workBlockSelection"]["candidateCount"], 2)

    def test_complete_work_block_can_update_human_commit_subject(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/subject-complete")
        start_work_block(repo, work_block_id="wb-subject-complete", actor="local-test")

        result = complete_work_block(
            repo,
            work_block_id="wb-subject-complete",
            finalize=False,
            summary="closeout: require human commit subjects",
        )

        self.assertEqual(result["status"], "completed")
        manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-subject-complete" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["commitSubject"], "closeout: require human commit subjects")

    def test_pre_response_broker_bootstrap_records_dirty_baseline_without_worktree(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/pre-response")
        (repo / "baseline-dirty.txt").write_text("dirty before hook\n", encoding="utf-8")
        worktrees_before = git(repo, "worktree", "list", "--porcelain").stdout

        result = bootstrap_response_broker_manifest(repo, hook_phase="response", actor="local-test-hook")

        worktrees_after = git(repo, "worktree", "list", "--porcelain").stdout
        manifest = result["manifest"]
        self.assertIn(result["status"], {"created", "refreshed"})
        self.assertEqual(manifest["branch"], "codex/pre-response")
        self.assertEqual(Path(manifest["worktree"]).resolve(), repo.resolve())
        self.assertEqual(manifest["startHead"], git(repo, "rev-parse", "HEAD").stdout.strip())
        self.assertIn("baseline-dirty.txt", manifest["dirtyBaseline"]["paths"])
        self.assertIn("lease", manifest)
        self.assertEqual(manifest["pathClaims"], [])
        self.assertEqual(worktrees_after, worktrees_before)

    def test_remediation_freeze_blocks_broker_bootstrap_lease_refresh_start_publish_finalize_and_hooks(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/freeze-guard")
        start_work_block(repo, work_block_id="wb-freeze", actor="local-test")
        manifest_path = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-freeze" / "manifest.json"
        lease_before = json.loads(manifest_path.read_text(encoding="utf-8"))["lease"]
        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")

        with self.assertRaises(Exception):
            start_work_block(repo, work_block_id="wb-blocked", actor="local-test")
        bootstrap = bootstrap_response_broker_manifest(repo, hook_phase="response", actor="local-test-hook")
        self.assertEqual(bootstrap["status"], "skipped")
        self.assertEqual(bootstrap["reason"], "remediation_freeze")
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["lease"], lease_before)
        self.assertEqual(repair_eligibility(repo, work_block_id="wb-freeze")["reason"], "remediation_freeze")
        self.assertEqual(finalize_work_block(repo, work_block_id="wb-freeze")["reason"], "remediation_freeze")
        for hook_name in ("pre-commit", "pre-push", "auto-closeout-hook"):
            with self.assertRaises(Exception):
                guard_closeout_hook(repo, hook_name=hook_name)

    def test_remediation_freeze_environment_is_process_scoped_and_fresh_preservation_worktree_is_exempt(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/freeze-env")
        config = load_closeout_config(repo)
        with mock.patch.dict(os.environ, {"CLOSEOUT_REMEDIATION_FREEZE": "1"}):
            self.assertTrue(remediation_freeze_status(repo, config)["active"])
        self.assertFalse(remediation_freeze_status(repo, config)["active"])

        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")
        fresh = self.tempdir / "target-preservation"
        git(repo, "worktree", "add", str(fresh), "master")
        self.write_config(fresh)
        self.assertTrue(remediation_freeze_status(repo, load_closeout_config(repo))["active"])
        self.assertFalse(remediation_freeze_status(fresh, load_closeout_config(fresh))["active"])

    def test_remediation_freeze_audit_packets_are_generated_exempt_and_content_addressed(self) -> None:
        repo = self.init_repo(remote=False)
        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")

        status = remediation_freeze_status(repo, load_closeout_config(repo), action="response-hook", write_audit_packet=True)

        packet = status["auditPacket"]
        self.assertTrue(packet["path"].startswith(".claude-state/closeout-log/remediation-freeze/sha256-"))
        self.assertTrue((repo / packet["path"]).exists())
        self.assertIn(".claude-state/**", load_closeout_config(repo)["paths"]["generated"])

    def test_already_integrated_dirty_baseline_overlap_enters_remediation_triage(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/already-integrated-dirty")
        (repo / "overlap.txt").write_text("dirty before broker\n", encoding="utf-8")
        start_work_block(repo, work_block_id="wb-integrated-dirty", actor="local-test", path_claims=["overlap.txt"])

        result = repair_eligibility(repo, work_block_id="wb-integrated-dirty")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["blockers"], ["dirty_state_remediation_required"])
        self.assertTrue((repo / ".claude-state" / "closeout-remediation.freeze").exists())
        packet_path = repo / result["freeze"]["packet"]["path"]
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["reason"], "dirty_state_remediation_required")
        self.assertEqual(packet["detection"]["workBlockId"], "wb-integrated-dirty")
        self.assertTrue(result["triage"]["ancestorOfTarget"])

    def test_remediation_packet_requires_stale_claim_proof_not_gone_upstream_only(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/stale-claim-packet")
        start_work_block(repo, work_block_id="wb-claim", actor="local-test", path_claims=["claimed.txt"])

        packet = remediation_packet_template(repo, load_closeout_config(repo), reason="stale_claim_remediation")
        policy = load_closeout_config(repo)["remediationFreeze"]

        self.assertEqual(packet["staleClaims"]["claimed.txt"], "wb-claim")
        self.assertTrue(policy["requireCoordinatorLock"])
        self.assertTrue(policy["requireHookGuardProof"])
        self.assertTrue(policy["requireRemoteAdvertisedPins"])
        self.assertIn("processQuiescence", packet)
        self.assertIn("hookGuardProof", packet)

    def test_remediation_preservation_requires_exact_allowlist_and_ref_or_bundle_backing(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/preservation-packet")
        (repo / "dirty.bin").write_bytes(b"\x00dirty bytes\n")

        packet = remediation_packet_template(
            repo,
            load_closeout_config(repo),
            reason="dirty_cluster_preservation",
            action_list=[{"actionId": "preserve_dirty_cluster", "paths": ["dirty.bin"]}],
        )
        policy = load_closeout_config(repo)["remediationFreeze"]
        dirty = {item["path"]: item for item in packet["dirtyPaths"]}

        self.assertTrue(policy["requireExactAllowlist"])
        self.assertTrue(policy["requireRecoveryBundle"])
        self.assertIn("dirty.bin", dirty)
        self.assertIsNotNone(dirty["dirty.bin"]["byteSha256"])
        self.assertIsNotNone(dirty["dirty.bin"]["gitObjectId"])
        self.assertIn("remoteAdvertisedTargetHead", packet["pinnedRefs"])
        self.assertEqual(packet["actionList"][0]["paths"], ["dirty.bin"])

    def test_remediation_freeze_removal_requires_coordinator_lock_quorum_and_revalidation(self) -> None:
        repo = self.init_repo(remote=False)
        config = load_closeout_config(repo)
        packet = remediation_packet_template(repo, config, reason="remove_remediation_freeze")

        self.assertTrue(config["remediationFreeze"]["requireCoordinatorLock"])
        self.assertIn("remove_remediation_freeze", config["reviewQuorum"]["highImpactActions"])
        self.assertIn("remediation_freeze_removal", config["autoQuorum"]["autonomousActionClasses"])
        self.assertEqual(packet["quorum"]["requiredReviewerScore"], 10)
        self.assertEqual(packet["quorum"]["requiredReviewers"], ["codex-self", "stranger-reviewer-1", "stranger-reviewer-2"])
        self.assertIn("remediationPacketHash", packet["quorum"]["tupleFields"])

    def test_remediation_hard_clean_blocks_remaining_freeze_marker(self) -> None:
        repo = self.init_repo(remote=False)
        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, load_closeout_config(repo), work_block_id=None)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("remediation_freeze_active", [item["kind"] for item in result["blockers"]])

    def test_remediation_freeze_remove_clears_marker_after_revalidation_and_quorum(self) -> None:
        repo = self.init_repo(remote=False)
        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{\"reason\":\"stale\"}\n", encoding="utf-8")

        result = remove_remediation_freeze(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["reason"], "remediation_freeze_removed")
        self.assertFalse(marker.exists())
        self.assertFalse(remediation_freeze_status(repo, load_closeout_config(repo))["active"])
        self.assertTrue(result["quorum"]["ok"])
        self.assertIn("quorumResult", result)
        self.assertEqual(result["quorumResult"]["packet"]["actionClass"], "remediation_freeze_removal")
        self.assertIn("remediation_freeze_removed", self.audit_types(repo))

    def test_remediation_freeze_remove_allows_stale_transaction_branches_after_revalidation(self) -> None:
        repo = self.init_repo(remote=False)
        marker = repo / ".claude-state" / "closeout-remediation.freeze"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{\"reason\":\"stale\"}\n", encoding="utf-8")

        postcondition = {
            "status": "blocked",
            "blockers": [
                {"kind": "remediation_freeze_active"},
                {"kind": "stale_transaction_branches", "branches": [{"branch": "codex/stale", "head": "abc123"}]},
            ],
            "closeoutCleanTruth": {
                "status": "blocked",
                "blockers": [
                    {"kind": "remediation_freeze_active"},
                    {"kind": "stale_transaction_branches", "branches": [{"branch": "codex/stale", "head": "abc123"}]},
                ],
            },
        }
        quorum_result = {
            "quorum": {"ok": True, "reason": None},
            "packet": {"actionClass": "remediation_freeze_removal", "unblockDetail": {"tupleHash": "tuple"}},
            "autoGenerated": True,
        }

        with mock.patch(
            "tools.repo_hygiene.brokered_closeout.repo_closed_postcondition_state",
            return_value=postcondition,
        ), mock.patch(
            "tools.repo_hygiene.brokered_closeout.ensure_autonomous_quorum",
            return_value=quorum_result,
        ):
            result = remove_remediation_freeze(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["reason"], "remediation_freeze_removed")
        self.assertFalse(marker.exists())
        self.assertFalse(remediation_freeze_status(repo, load_closeout_config(repo))["active"])
        self.assertTrue(result["quorum"]["ok"])
        self.assertTrue(result["quorumResult"]["autoGenerated"])

    def test_remediation_freeze_remove_blocks_fresh_dirty_marker(self) -> None:
        repo = self.init_repo(remote=False)
        config = load_closeout_config(repo)
        dirty_path = repo / "dirty.txt"
        dirty_path.write_text("same dirty state\n", encoding="utf-8")
        enter_remediation_freeze(repo, config, reason="dirty_state_remediation_required")

        stale = remediation_freeze_staleness(repo, config)
        result = remove_remediation_freeze(repo)

        self.assertFalse(stale["stale"], stale)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "repo_closed_postcondition_failed")
        self.assertFalse(result["staleFreezeEvidence"]["stale"], result)
        self.assertTrue((repo / ".claude-state" / "closeout-remediation.freeze").exists())
        self.assertIn("non_exempt_untracked_files", [item["kind"] for item in result["postcondition"]["blockers"]])

    def test_remediation_freeze_remove_allows_stale_dirty_marker_after_quorum(self) -> None:
        repo = self.init_repo(remote=False)
        config = load_closeout_config(repo)
        stale_path = repo / "stale.txt"
        stale_path.write_text("stale dirty state\n", encoding="utf-8")
        enter_remediation_freeze(repo, config, reason="dirty_state_remediation_required")
        stale_path.unlink()
        (repo / "current.txt").write_text("current dirty state\n", encoding="utf-8")

        result = remove_remediation_freeze(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["reason"], "remediation_freeze_removed")
        self.assertTrue(result["staleFreezeEvidence"]["stale"], result)
        self.assertIn("dirty_state_mismatch", [item["kind"] for item in result["staleFreezeEvidence"]["reasons"]])
        self.assertFalse((repo / ".claude-state" / "closeout-remediation.freeze").exists())
        self.assertFalse(remediation_freeze_status(repo, load_closeout_config(repo))["active"])
        self.assertTrue(result["quorum"]["ok"])

    def test_remediation_freeze_remove_allows_wrong_work_block_marker_after_quorum(self) -> None:
        repo = self.init_repo(remote=False)
        config = load_closeout_config(repo)
        git(repo, "checkout", "-b", "codex/wrong-freeze-owner")
        start_work_block(repo, work_block_id="wb-right", actor="local-test")
        (repo / "dirty.txt").write_text("current dirty state\n", encoding="utf-8")
        detection = detect_work_block(repo, work_block_id="wb-right")
        detection["workBlockId"] = "wb-wrong"
        enter_remediation_freeze(
            repo,
            config,
            reason="dirty_state_remediation_required",
            detection=detection,
        )

        stale = remediation_freeze_staleness(repo, config, work_block_id="wb-right")
        result = remove_remediation_freeze(repo, work_block_id="wb-right")

        self.assertTrue(stale["stale"], stale)
        self.assertIn("work_block_id_mismatch", [item["kind"] for item in stale["reasons"]])
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["reason"], "remediation_freeze_removed")
        self.assertTrue(result["staleFreezeEvidence"]["stale"], result)
        self.assertIn("work_block_id_mismatch", [item["kind"] for item in result["staleFreezeEvidence"]["reasons"]])
        self.assertFalse((repo / ".claude-state" / "closeout-remediation.freeze").exists())
        self.assertFalse(remediation_freeze_status(repo, config)["active"])

    def test_clean_at_start_new_dirty_paths_auto_claimed_and_checkpointed_through_quorum(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/clean-at-start")
        start_work_block(repo, work_block_id="wb-clean-start", actor="local-test")
        (repo / "new-owned.txt").write_text("owned after baseline\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-clean-start")
        self.assertEqual([item["path"] for item in detection["ownedDirty"]], ["new-owned.txt"])
        self.assertTrue(detection["ownedDirty"][0]["autoClaimedByDirtyBaseline"])

        result = finalize_work_block(repo, work_block_id="wb-clean-start")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(git(repo, "show", "master:new-owned.txt").stdout, "owned after baseline\n")
        self.assertIn("checkpoint_owned_dirty", self.audit_types(repo))
        self.assertIn("auto_quorum", self.audit_types(repo))

    def test_existing_analysis_note_clean_at_start_is_auto_claimed_despite_sensitive_root(self) -> None:
        repo = self.init_repo(remote=False)
        note = repo / ".claude" / "analysis" / "mlv-playback-investigation.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# Existing note\n", encoding="utf-8")
        git(repo, "add", ".claude/analysis/mlv-playback-investigation.md")
        git(repo, "commit", "-m", "add curated analysis note")
        git(repo, "checkout", "-b", "codex/analysis-note-update")
        start_work_block(repo, work_block_id="wb-analysis-note", actor="local-test")
        note.write_text("# Existing note\n\nFollow-up finding.\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-analysis-note")

        self.assertEqual([item["path"] for item in detection["ownedDirty"]], [".claude/analysis/mlv-playback-investigation.md"])
        self.assertFalse(detection["unownedDirty"], detection)
        self.assertTrue(detection["ownedDirty"][0]["sensitive"])
        self.assertTrue(detection["ownedDirty"][0]["cleanAtStartSensitiveAutoClaimAllowed"])
        self.assertEqual(
            detection["ownedDirty"][0]["classificationReason"],
            "tracked sensitive note was clean or absent at broker dirty baseline",
        )

    def test_new_analysis_note_under_sensitive_root_still_blocks_as_unowned(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/new-analysis-note")
        start_work_block(repo, work_block_id="wb-new-analysis-note", actor="local-test")
        note = repo / ".claude" / "analysis" / "new-note.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("new ad-hoc note\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-new-analysis-note")

        self.assertFalse(detection["ownedDirty"], detection)
        self.assertEqual([item["path"] for item in detection["unownedDirty"]], [".claude/analysis/new-note.md"])
        self.assertTrue(detection["unownedDirty"][0]["sensitive"])
        self.assertFalse(detection["unownedDirty"][0]["cleanAtStartSensitiveAutoClaimAllowed"])

    def test_stale_integrated_claims_do_not_foreignize_clean_at_start_dirty(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/stale-claim-owner")
        start_work_block(
            repo,
            work_block_id="wb-stale-claim-owner",
            actor="local-test",
            path_claims=["closeout.config.json"],
        )
        stale_manifest = repo / ".claude-state" / "closeout" / "work-blocks" / "wb-stale-claim-owner" / "manifest.json"
        stale_data = json.loads(stale_manifest.read_text(encoding="utf-8"))
        stale_data["state"] = "blocked"
        stale_data["blockedReason"] = "historical_owner_already_integrated"
        stale_manifest.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
        git(repo, "checkout", "master")
        git(repo, "checkout", "-b", "codex/current-config-repair")
        start_work_block(repo, work_block_id="wb-current-config-repair", actor="local-test")
        config_path = repo / "closeout.config.json"
        config_path.write_text(config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-current-config-repair")

        self.assertEqual([item["path"] for item in detection["ownedDirty"]], ["closeout.config.json"])
        self.assertEqual(detection["foreignDirty"], [])

    def test_generated_closeout_conflict_packets_are_not_owned_dirty(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/generated-packet")
        packet = ".claude-state/closeout/repo-sweep/conflicts/runtime-packet.json"
        start_work_block(repo, work_block_id="wb-generated-packet", actor="local-test", path_claims=[packet])
        packet_path = repo / packet
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text('{"state":"tracked"}\n', encoding="utf-8")
        git(repo, "add", "-f", packet)
        git(repo, "commit", "-m", "track generated conflict packet")
        packet_path.write_text('{"state":"runtime update"}\n', encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-generated-packet")

        self.assertFalse(detection["ownedDirty"], detection)
        self.assertFalse(detection["mixedDirty"], detection)
        self.assertFalse(detection["unownedDirty"], detection)
        self.assertEqual([item["path"] for item in detection["foreignDirty"]], [packet])
        self.assertTrue(detection["eligible"], detection)

    def test_baseline_dirty_claimed_path_blocks_as_mixed_and_not_checkpointed(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/mixed-baseline")
        (repo / "mixed.txt").write_text("dirty before broker\n", encoding="utf-8")
        start_work_block(repo, work_block_id="wb-mixed", actor="local-test", path_claims=["mixed.txt"])
        head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

        detection = detect_work_block(repo, work_block_id="wb-mixed")
        self.assertFalse(detection["ownedDirty"], detection)
        self.assertEqual([item["path"] for item in detection["mixedDirty"]], ["mixed.txt"])
        self.assertEqual(detection["mixedDirty"][0]["blocker"], "baseline-dirty-overlaps-candidate")

        result = checkpoint_owned_work(repo, work_block_id="wb-mixed")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "baseline-dirty-overlaps-candidate")
        self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout, "")
        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)

    def test_baseline_dirty_changed_after_start_blocks_as_mixed_without_claim(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/baseline-changed")
        (repo / "mixed.txt").write_text("dirty before broker\n", encoding="utf-8")
        start_work_block(repo, work_block_id="wb-baseline-changed", actor="local-test")
        head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "mixed.txt").write_text("dirty changed during block\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-baseline-changed")

        self.assertFalse(detection["ownedDirty"], detection)
        self.assertFalse(detection["foreignDirty"], detection)
        self.assertEqual([item["path"] for item in detection["mixedDirty"]], ["mixed.txt"])
        self.assertTrue(detection["mixedDirty"][0]["dirtyBaselineContentChanged"])
        self.assertEqual(detection["mixedDirty"][0]["blocker"], "baseline-dirty-overlaps-candidate")

        result = checkpoint_owned_work(repo, work_block_id="wb-baseline-changed")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "baseline-dirty-overlaps-candidate")
        self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout, "")
        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)

    def test_owned_dirty_checkpoint_stages_only_exact_owned_paths(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/exact-checkpoint")
        start_work_block(repo, work_block_id="wb-exact", actor="local-test")
        start_work_block(repo, work_block_id="wb-other-foreign", actor="local-test", path_claims=["foreign.txt"])
        (repo / "owned.txt").write_text("owned checkpoint\n", encoding="utf-8")
        (repo / "foreign.txt").write_text("foreign stays dirty\n", encoding="utf-8")

        result = checkpoint_owned_work(repo, work_block_id="wb-exact")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["paths"], ["owned.txt"])
        self.assertEqual(result["stagedPaths"], ["owned.txt"])
        self.assertEqual(git(repo, "show", "HEAD:owned.txt").stdout, "owned checkpoint\n")
        self.assertEqual(git(repo, "show", "HEAD:foreign.txt").stdout, "base foreign\n")
        self.assertEqual((repo / "foreign.txt").read_text(encoding="utf-8"), "foreign stays dirty\n")
        self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout, "")

    def test_owned_dirty_checkpoint_stages_exact_owned_deletion(self) -> None:
        repo = self.init_repo(remote=False)
        git(repo, "checkout", "-b", "codex/deletion-checkpoint")
        deleted_path = repo / "obsolete-receipt.json"
        deleted_path.write_text('{"state":"superseded"}\n', encoding="utf-8")
        git(repo, "add", deleted_path.name)
        git(repo, "commit", "-m", "add superseded receipt")
        start_work_block(repo, work_block_id="wb-delete", actor="local-test")
        deleted_path.unlink()
        git(repo, "add", "-A", "--", deleted_path.name)

        result = checkpoint_owned_work(repo, work_block_id="wb-delete")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["paths"], [deleted_path.name])
        self.assertEqual(result["stagedPaths"], [deleted_path.name])
        self.assertNotEqual(
            git(repo, "cat-file", "-e", f"HEAD:{deleted_path.name}", check=False).returncode,
            0,
        )
        self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout, "")

    def test_stale_refs_block_finalize_before_mutation(self) -> None:
        repo = self.init_repo()
        self.make_feature(repo, "wb-stale-refs")
        detection = detect_work_block(repo, work_block_id="wb-stale-refs")
        (repo / "work.txt").write_text("feature work\nchanged\n", encoding="utf-8")
        git(repo, "add", "work.txt")
        git(repo, "commit", "-m", "move feature head")
        result = finalize_work_block(repo, work_block_id="wb-stale-refs", expected_pinned_refs=detection["pinnedRefs"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "stale_refs")
        self.assertIn("stale_refs", self.audit_types(repo))
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "codex/test-work")

    def test_finalize_blocks_work_block_started_from_unintegrated_base(self) -> None:
        repo = self.init_repo()
        target_head = git(repo, "rev-parse", "master").stdout.strip()
        git(repo, "checkout", "-b", "codex/unapproved-sibling", "master")
        (repo / "e2.txt").write_text("unapproved sibling phase\n", encoding="utf-8")
        git(repo, "add", "e2.txt")
        git(repo, "commit", "-m", "unapproved sibling phase")
        unapproved_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        start_work_block(repo, work_block_id="wb-stacked", actor="local-test", path_claims=["tool.txt"])
        (repo / "tool.txt").write_text("stacked work\n", encoding="utf-8")
        git(repo, "add", "tool.txt")
        git(repo, "commit", "-m", "stacked work")

        result = finalize_work_block(repo, work_block_id="wb-stacked")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "work_block_base_not_integrated")
        self.assertEqual(result["startHead"], unapproved_head)
        self.assertEqual(result["targetHead"], target_head)
        self.assertIn("work_block_base_not_integrated", self.audit_types(repo))
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), target_head)
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:e2.txt", check=False).returncode, 0)
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:tool.txt", check=False).returncode, 0)

    def test_canonical_worktree_root_is_stable_across_worktrees_and_subdirectories(self) -> None:
        """GATE-ID-4. The ledger root must be the MAIN worktree for every caller.

        Four live instances of the disease are on record: a work-block worktree is a checkout
        of tracked files only, `.claude-state/` is gitignored, so the gate resolved a ledger
        that did not exist -- or was satisfied by a per-worktree COPY, which means a verdict
        could be written where the next block would never read it.

        The subdirectory cases are the ones that matter most: `--git-common-dir` WITHOUT
        `--path-format=absolute` returns `../.git` from a subdirectory, so resolving it
        against the wrong base silently yields a wrong ledger -- the same cwd-dependence this
        change exists to remove.
        """
        repo = self.init_repo()
        linked = repo.parent / "gate-id-4-linked"
        git(repo, "worktree", "add", "--detach", str(linked), "HEAD")

        (repo / "sub" / "deeper").mkdir(parents=True, exist_ok=True)
        (linked / "sub").mkdir(parents=True, exist_ok=True)

        expected = repo.resolve()
        for label, start in (
            ("main root", repo),
            ("main subdirectory", repo / "sub"),
            ("main nested subdirectory", repo / "sub" / "deeper"),
            ("linked worktree root", linked),
            ("linked worktree subdirectory", linked / "sub"),
        ):
            self.assertEqual(
                canonical_worktree_root(start).resolve(),
                expected,
                "%s must resolve to the main worktree root" % label,
            )

    def test_canonical_worktree_root_of_a_non_repository_is_the_path_itself(self) -> None:
        """The two failure cases are deliberately NOT the same.

        Outside a repository no linked worktree can exist, so the defect is impossible and
        the path itself IS the canonical root -- the same answer by a different route, not a
        silent fallback to per-worktree behaviour. This is also what lets the ledger-parsing
        tests exercise the validator against a plain directory.

        The other case -- a real repository whose common dir cannot be resolved absolutely --
        returns None and the caller fails closed, because there the correct root is genuinely
        unknown and guessing would restore the defect while appearing fixed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp)
            self.assertFalse((plain / ".git").exists())
            self.assertEqual(canonical_worktree_root(plain), plain)

    def test_review_gate_reads_the_canonical_ledger_from_a_worktree_without_claude_state(self) -> None:
        """GATE-ID-4, end to end: the exact shape that blocked four live work blocks.

        The linked worktree has NO `.claude-state/` at all -- that is not contrived, it is
        what every fresh work-block worktree looks like, because the directory is gitignored.
        Before this change the gate resolved the ledger inside that worktree and blocked
        `content_approval_file_missing`. It must now read the one canonical ledger.
        """
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-gate-id-4")
        detection = detect_work_block(repo, work_block_id="wb-gate-id-4")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        # The verdict is written ONLY to the canonical ledger.
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n\nbody\n\n"
            "### [2026-01-01T01:00:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\nVerdict: APPROVE\n"
            "Seat: 5fc3fc6e-345f-40b8-bb3d-7abd6302b459\n\nbody\n",
        )

        linked = repo.parent / "gate-id-4-nostate"
        git(repo, "worktree", "add", "--detach", str(linked), detection["featureHead"])
        self.assertFalse((linked / ".claude-state").exists(),
                         "the fixture must reproduce the real shape: no .claude-state in the worktree")

        config = load_closeout_config(linked)
        result = validate_content_review_approval_for_finalize(linked, config, detection, {"startHead": detection["targetHead"]})
        self.assertIsNone(result, result)

    def test_review_gate_still_blocks_from_a_worktree_when_the_canonical_ledger_lacks_approval(self) -> None:
        """The falsifier for the test above: resolving to the canonical ledger must not make
        the gate permissive. Same worktree, same absent `.claude-state`, handoff only."""
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-gate-id-4-neg")
        detection = detect_work_block(repo, work_block_id="wb-gate-id-4-neg")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n\nbody\n",
        )

        linked = repo.parent / "gate-id-4-neg"
        git(repo, "worktree", "add", "--detach", str(linked), detection["featureHead"])

        config = load_closeout_config(linked)
        result = validate_content_review_approval_for_finalize(linked, config, detection, {"startHead": detection["targetHead"]})
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_missing")

    def test_finalize_blocks_review_gated_chunk_without_claude_approval(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-gated")
        detection = detect_work_block(repo, work_block_id="wb-review-gated")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-gated")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "content_approval_missing")
        self.assertEqual(result["range"], range_token)
        self.assertIn("content_approval_missing", self.audit_types(repo))
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:work.txt", check=False).returncode, 0)

    def test_finalize_rejects_claude_approval_for_different_range(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-wrong-range")
        detection = detect_work_block(repo, work_block_id="wb-review-wrong-range")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        wrong_range = "%s..%s" % (detection["targetHead"], "0" * 40)
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n\n"
            "### [2026-01-01T00:01:00Z] CLAUDE - REVIEW\n"
            f"Range: `{wrong_range}`\n"
            "Verdict: APPROVE\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-wrong-range")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "content_approval_missing")
        self.assertIn("content_approval_missing", self.audit_types(repo))
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:work.txt", check=False).returncode, 0)

    def test_finalize_blocks_when_latest_claude_review_is_hold(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-hold")
        detection = detect_work_block(repo, work_block_id="wb-review-hold")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n\n"
            "### [2026-01-01T00:01:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\n"
            "Verdict: HOLD\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-hold")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "content_approval_not_approved")
        self.assertIn("content_approval_not_approved", self.audit_types(repo))
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:work.txt", check=False).returncode, 0)

    def test_finalize_accepts_latest_claude_approval_for_handed_off_range(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-approved")
        detection = detect_work_block(repo, work_block_id="wb-review-approved")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n\n"
            "### [2026-01-01T00:01:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\n"
            "Verdict: APPROVE\n"
            "Seat: 5fc3fc6e-345f-40b8-bb3d-7abd6302b459\n\n"
            "### [2026-01-01T00:02:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\n"
            "Verdict: IDLE\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-approved")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(git(repo, "show", "master:work.txt").stdout, "feature work\n")

    def test_finalize_blocks_when_latest_claude_review_requests_changes(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-changes")
        detection = detect_work_block(repo, work_block_id="wb-review-changes")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n\n"
            "### [2026-01-01T00:01:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\n"
            "Verdict: APPROVE\n\n"
            "### [2026-01-01T00:02:00Z] CLAUDE - REVIEW\n"
            f"Range: `{range_token}`\n"
            "Verdict: CHANGES_REQUESTED\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-changes")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "content_approval_blocked")
        self.assertIn("content_approval_blocked", self.audit_types(repo))
        self.assertNotEqual(git(repo, "cat-file", "-e", "master:work.txt", check=False).returncode, 0)

    def test_content_review_gate_surfaces_expected_format_for_configured_actors(self) -> None:
        repo = self.init_repo(config_updates={"contentReviewGate": {"requireClaudeApprovalForFinalize": True}})
        self.make_feature(repo, "wb-review-format")
        detection = detect_work_block(repo, work_block_id="wb-review-format")
        range_token = "%s..%s" % (detection["targetHead"], detection["featureHead"])
        self.write_gpu_lane_coordination(
            repo,
            "### [2026-01-01T00:00:00Z] CODEX - HANDOFF\n"
            f"Range: `{range_token}`\n"
            "Gate: ready for Claude review.\n",
        )

        result = finalize_work_block(repo, work_block_id="wb-review-format")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "content_approval_missing")
        fmt = result["expectedEntryFormat"]
        self.assertEqual(fmt["canonicalRange"], range_token)
        self.assertEqual(fmt["handoffHeadingContains"], ["CODEX", "HANDOFF"])
        self.assertEqual(fmt["reviewHeadingContains"], ["CLAUDE", "REVIEW"])
        self.assertEqual(fmt["requiredRangeLine"], "Range: `%s`" % range_token)
        self.assertEqual(fmt["requiredVerdictLine"], "Verdict: APPROVE")
        # Recovery guidance must reflect the configured direction: CODEX hands off, CLAUDE reviews.
        self.assertIn("CODEX HANDOFF", result["recoveryCommand"])
        self.assertIn("CLAUDE REVIEW", result["recoveryCommand"])

    def test_stale_review_tuple_blocks_when_target_moves(self) -> None:
        repo = self.init_repo(config_updates={"autoQuorum": {"allowStaleReviewRenewal": False}})
        self.make_feature(repo, "wb-stale-review")
        self.approve_current_tuple(repo, "wb-stale-review")
        git(repo, "checkout", "master")
        (repo / "target.txt").write_text("target moved\n", encoding="utf-8")
        git(repo, "add", "target.txt")
        git(repo, "commit", "-m", "move target")
        git(repo, "checkout", "codex/test-work")
        result = finalize_work_block(repo, work_block_id="wb-stale-review")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "stale_review")
        self.assertIn("stale_review", self.audit_types(repo))
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "codex/test-work")

    def test_finalize_auto_quorum_renews_stale_review_when_policy_allows(self) -> None:
        repo = self.init_repo()
        self.make_feature(repo, "wb-renew-stale-review")
        self.approve_current_tuple(repo, "wb-renew-stale-review")
        (repo / "feature-followup.txt").write_text("follow-up after review\n", encoding="utf-8")
        git(repo, "add", "feature-followup.txt")
        git(repo, "commit", "-m", "feature follow-up")

        result = finalize_work_block(repo, work_block_id="wb-renew-stale-review")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["quorum"]["matchingApprovals"], 3)
        self.assertIn("auto_quorum", self.audit_types(repo))
        self.assertEqual(git(repo, "show", "master:feature-followup.txt").stdout, "follow-up after review\n")

    def test_validation_failure_blocks_after_clean_merge(self) -> None:
        repo = self.init_repo(
            config_updates={
                "validation": {
                    "commands": [
                        {"name": "intentional-failure", "argv": [sys.executable, "-c", "import sys; sys.exit(7)"]}
                    ]
                }
            }
        )
        self.make_feature(repo, "wb-validation")
        self.approve_current_tuple(repo, "wb-validation")
        result = finalize_work_block(repo, work_block_id="wb-validation")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "validation_failed")
        self.assertIn("validation_failure", self.audit_types(repo))
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/test-work").returncode, 0)

    def test_closeout_tooling_stale_blocks_before_hygiene_blocker(self) -> None:
        repo = self.init_repo(
            config_updates={
                "toolingBaseline": {
                    "enabled": True,
                    "autoUpdate": False,
                    "requiredTests": ["test_missing_future_closeout_actor"],
                    "requiredSymbols": [
                        {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def missing_future_actor"}
                    ],
                }
            }
        )
        self.make_feature(repo, "wb-tooling-stale")
        (repo / "work.txt").write_text("dirty blocker should not be authoritative\n", encoding="utf-8")

        result = finalize_work_block(repo, work_block_id="wb-tooling-stale")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "closeout_tooling_stale")
        missing_kinds = {item["kind"] for item in result["tooling"]["missing"]}
        self.assertIn("test", missing_kinds)
        self.assertIn("symbol", missing_kinds)
        self.assertIn("closeout_tooling_stale", self.audit_types(repo))

    def test_tooling_baseline_check_is_plan_only_during_finalize(self) -> None:
        repo = self.init_repo(
            config_updates={
                "toolingBaseline": {
                    "enabled": True,
                    "baselineRef": "master",
                    "autoUpdate": True,
                    "paths": ["tools/repo_hygiene/brokered_closeout.py"],
                    "requiredTests": [],
                    "requiredSymbols": [
                        {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def baseline_actor"}
                    ],
                }
            }
        )
        tool_path = repo / "tools" / "repo_hygiene" / "brokered_closeout.py"
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_text("def baseline_actor():\n    return True\n", encoding="utf-8")
        git(repo, "add", "tools/repo_hygiene/brokered_closeout.py")
        git(repo, "commit", "-m", "baseline tooling")
        git(repo, "checkout", "-b", "codex/stale-tooling")
        tool_path.write_text("def old_actor():\n    return False\n", encoding="utf-8")
        git(repo, "add", "tools/repo_hygiene/brokered_closeout.py")
        git(repo, "commit", "-m", "stale tooling")
        start_work_block(repo, work_block_id="wb-tooling-update", actor="local-test", path_claims=["work.txt"])

        result = repair_eligibility(repo, work_block_id="wb-tooling-update")

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "closeout_tooling_stale")
        self.assertIn("def old_actor", tool_path.read_text(encoding="utf-8"))
        self.assertFalse(result["tooling"]["updated"])
        self.assertEqual(
            result["tooling"]["plannedUpdates"],
            [{"path": "tools/repo_hygiene/brokered_closeout.py", "baselineRef": "master"}],
        )
        self.assertEqual(git(repo, "status", "--short").stdout, "")

    def test_foreign_dirty_remains_retained_audited_and_does_not_block_independent_closeout(self) -> None:
        repo = self.init_repo()
        self.make_feature(repo, "wb-foreign")
        start_work_block(repo, work_block_id="wb-foreign-owner", actor="local-test", path_claims=["foreign.txt"])
        (repo / "foreign.txt").write_text("dirty but unrelated\n", encoding="utf-8")
        detection = detect_work_block(repo, work_block_id="wb-foreign")
        self.assertFalse(detection["ownedDirty"])
        self.assertFalse(detection["unownedDirty"])
        self.assertEqual([item["path"] for item in detection["foreignDirty"]], ["foreign.txt"])
        self.approve_current_tuple(repo, "wb-foreign")
        result = finalize_work_block(repo, work_block_id="wb-foreign")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["repoClosedPostcondition"]["ok"])
        self.assertEqual(result["repoClosedPostcondition"]["status"], "success")
        self.assertFalse(result["repoClosedPostcondition"]["closeoutCleanTruth"]["rawGit"]["clean"])
        self.assertTrue(result["repoClosedPostcondition"]["closeoutCleanTruth"]["policy"]["retainedForeignDirtyAllowed"])
        self.assertEqual(result["repoClosedPostcondition"]["closeoutCleanTruth"]["status"], "clean")
        self.assertEqual([item["path"] for item in result["foreignDirtyRetained"]], ["foreign.txt"])
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/test-work", check=False).returncode, 0)
        self.assertEqual((repo / "foreign.txt").read_text(encoding="utf-8"), "dirty but unrelated\n")
        self.assertIn("success", self.audit_types(repo))
        self.assertIn("branch_deletion", self.audit_types(repo))

    def test_other_work_block_claim_takes_precedence_over_branch_delta(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/claimed-delta")
        start_work_block(repo, work_block_id="wb-current", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        start_work_block(repo, work_block_id="wb-z-other", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("other dirty\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-current")

        self.assertFalse(detection["ownedDirty"], detection)
        self.assertEqual([item["path"] for item in detection["foreignDirty"]], ["owned.txt"])
        self.assertEqual(detection["foreignDirty"][0]["ownerWorkBlockId"], "wb-z-other")

    def test_overlapping_claim_owner_is_stable_across_manifest_directory_order(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/claimed-delta")
        start_work_block(repo, work_block_id="wb-current", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        start_work_block(repo, work_block_id="wb-z-other", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("other dirty\n", encoding="utf-8")

        original_glob = Path.glob
        owners = []
        for reverse in (False, True):
            def ordered_glob(self, pattern, *args, _reverse=reverse, **kwargs):
                return iter(sorted(original_glob(self, pattern, *args, **kwargs), reverse=_reverse))

            with mock.patch.object(Path, "glob", ordered_glob):
                detection = detect_work_block(repo, work_block_id="wb-current")
            owners.append(
                {
                    "reverse": reverse,
                    "ownedDirty": [item["path"] for item in detection["ownedDirty"]],
                    "foreignOwners": [item["ownerWorkBlockId"] for item in detection["foreignDirty"]],
                }
            )

        self.assertEqual([item["foreignOwners"] for item in owners], [["wb-z-other"], ["wb-z-other"]], owners)
        self.assertEqual([item["ownedDirty"] for item in owners], [[], []], owners)

    def test_newer_claim_wins_when_work_block_id_sorts_against_recency(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/claimed-delta")
        # Pin the second-truncated broker clock so both work blocks start inside
        # the same second. That is the case where updatedAt ties and precedence
        # silently falls through to the workBlockId tiebreak; left to wall-clock
        # it depends on whether the two starts straddle a second boundary.
        with mock.patch("tools.repo_hygiene.brokered_closeout.utc_now", return_value="2026-07-26T00:00:01+00:00"):
            start_work_block(repo, work_block_id="wb-current", actor="local-test", path_claims=["owned.txt"])
            (repo / "owned.txt").write_text("committed\n", encoding="utf-8")
            git(repo, "add", "owned.txt")
            git(repo, "commit", "-m", "owned committed")
            # "wb-a-other" sorts BEFORE "wb-current", so a workBlockId tiebreak
            # hands the newer block's uncommitted bytes to the older block and
            # reopens the cross-block capture path. Plain glob order throughout.
            start_work_block(repo, work_block_id="wb-a-other", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("other dirty\n", encoding="utf-8")

        detection = detect_work_block(repo, work_block_id="wb-current")

        self.assertFalse(detection["ownedDirty"], detection)
        self.assertEqual([item["path"] for item in detection["foreignDirty"]], ["owned.txt"])
        self.assertEqual(detection["foreignDirty"][0]["ownerWorkBlockId"], "wb-a-other")

    def test_work_block_recency_prefers_precise_stamp_and_falls_back(self) -> None:
        from .brokered_closeout import work_block_recency

        precise = {"updatedAt": "2026-07-26T00:00:01+00:00", "updatedAtPrecise": "2026-07-26T00:00:01.500000+00:00"}
        coarse = {"updatedAt": "2026-07-26T00:00:01+00:00"}
        legacy = {"startedAt": "2026-07-26T00:00:01+00:00"}
        self.assertGreater(work_block_recency(precise), work_block_recency(coarse))
        self.assertEqual(work_block_recency(coarse), work_block_recency(legacy))
        self.assertEqual(work_block_recency({}), datetime.min.replace(tzinfo=timezone.utc))

    def test_no_origin_local_only_closeout_updates_target_and_prunes_branch(self) -> None:
        repo = self.init_repo(remote=False)
        self.make_feature(repo, "wb-local-only")
        self.approve_current_tuple(repo, "wb-local-only")
        result = finalize_work_block(repo, work_block_id="wb-local-only")
        self.assertEqual(result["status"], "success")
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")
        self.assertEqual((repo / "work.txt").read_text(encoding="utf-8"), "feature work\n")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/test-work", check=False).returncode, 0)

    def test_finalize_auto_quorum_resolves_missing_review(self) -> None:
        repo = self.init_repo(remote=False)
        self.make_feature(repo, "wb-auto-finalize")

        result = finalize_work_block(repo, work_block_id="wb-auto-finalize")

        self.assertEqual(result["status"], "success", result)
        self.assertIn("auto_quorum", self.audit_types(repo))
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")

    def test_partial_push_recovery_updates_local_target_and_cleans_branch(self) -> None:
        repo = self.init_repo(remote=True)
        self.make_feature(repo, "wb-partial-push")
        feature_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", "HEAD:master")
        local_master_before = git(repo, "rev-parse", "master").stdout.strip()
        self.assertNotEqual(local_master_before, feature_head)
        self.approve_current_tuple(repo, "wb-partial-push")
        result = finalize_work_block(repo, work_block_id="wb-partial-push")
        self.assertEqual(result["status"], "success")
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), feature_head)
        self.assertIn("partial_push_recovery", self.audit_types(repo))
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/test-work", check=False).returncode, 0)

    def test_local_target_update_fast_forwards_checked_out_worktree_and_refreshes_files(self) -> None:
        repo = self.init_repo(remote=False)
        base_head = git(repo, "rev-parse", "master").stdout.strip()
        git(repo, "checkout", "-b", "codex/target-sync", base_head)
        target_worktree = self.tempdir / "target-master-worktree"
        git(repo, "worktree", "add", str(target_worktree), "master")
        (repo / "synced.txt").write_text("new target content\n", encoding="utf-8")
        git(repo, "add", "synced.txt")
        git(repo, "commit", "-m", "advance target")
        new_head = git(repo, "rev-parse", "HEAD").stdout.strip()

        preflight = target_worktree_update_preflight(repo, "master", new_head)
        result = update_local_target(repo, "master", new_head)

        self.assertEqual(preflight["status"], "success", preflight)
        self.assertEqual(preflight["method"], "checked-out-worktree-ff")
        self.assertEqual(result["returncode"], 0, result)
        self.assertTrue(result["verified"], result)
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), new_head)
        self.assertEqual((target_worktree / "synced.txt").read_text(encoding="utf-8"), "new target content\n")
        self.assertEqual(git(target_worktree, "status", "--porcelain").stdout, "")

    def test_local_target_update_refuses_dirty_checked_out_worktree_without_moving_ref(self) -> None:
        repo = self.init_repo(remote=False)
        base_head = git(repo, "rev-parse", "master").stdout.strip()
        git(repo, "checkout", "-b", "codex/target-sync-dirty", base_head)
        target_worktree = self.tempdir / "dirty-target-master-worktree"
        git(repo, "worktree", "add", str(target_worktree), "master")
        (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
        git(repo, "add", "advance.txt")
        git(repo, "commit", "-m", "advance target candidate")
        new_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        (target_worktree / "README.md").write_text("local owner edit\n", encoding="utf-8")

        result = update_local_target(repo, "master", new_head)

        self.assertNotEqual(result["returncode"], 0, result)
        self.assertEqual(result["reason"], "target_worktree_dirty_before_ref_update")
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), base_head)
        self.assertEqual((target_worktree / "README.md").read_text(encoding="utf-8"), "local owner edit\n")

    def test_finalize_blocks_before_remote_push_when_checked_out_target_worktree_is_dirty(self) -> None:
        repo = self.init_repo(remote=True)
        self.make_feature(repo, "wb-dirty-target-prepush")
        target_worktree = self.tempdir / "dirty-target-before-push"
        git(repo, "worktree", "add", str(target_worktree), "master")
        (target_worktree / "README.md").write_text("local owner edit\n", encoding="utf-8")
        remote_before = git(repo, "ls-remote", "origin", "refs/heads/master").stdout.split()[0]
        self.approve_current_tuple(repo, "wb-dirty-target-prepush")

        result = finalize_work_block(repo, work_block_id="wb-dirty-target-prepush")

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "target_worktree_not_ready_before_push")
        self.assertEqual(result["detail"]["reason"], "target_worktree_dirty_before_ref_update")
        remote_after = git(repo, "ls-remote", "origin", "refs/heads/master").stdout.split()[0]
        self.assertEqual(remote_after, remote_before)
        self.assertEqual((target_worktree / "README.md").read_text(encoding="utf-8"), "local owner edit\n")

    def test_target_push_non_fast_forward_fetches_updates_local_target_and_reports_rerun(self) -> None:
        repo = self.init_repo(remote=True)
        base_master = git(repo, "rev-parse", "master").stdout.strip()
        git(repo, "checkout", "-b", "codex/attempted-target", "master")
        (repo / "attempted.txt").write_text("attempted closeout merge\n", encoding="utf-8")
        git(repo, "add", "attempted.txt")
        git(repo, "commit", "-m", "attempted target merge")
        attempted_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "-b", "codex/remote-move", "master")
        (repo / "remote.txt").write_text("other closeout won first\n", encoding="utf-8")
        git(repo, "add", "remote.txt")
        git(repo, "commit", "-m", "move remote target")
        remote_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", "HEAD:master")
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), base_master)

        result = repair_target_push_failure(
            repo,
            load_closeout_config(repo),
            target_branch="master",
            remote="origin",
            attempted_head=attempted_head,
            push_result={
                "remote": "origin",
                "targetBranch": "master",
                "returncode": 1,
                "stdout": "",
                "stderr": "! [rejected] HEAD -> master (fetch first)\nerror: failed to push some refs\nhint: Updates were rejected because the remote contains work that you do not have locally.\n",
            },
            work_block_id="wb-non-ff",
        )

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "target_push_rerun_required")
        self.assertEqual(result["attemptedHead"], attempted_head)
        self.assertEqual(result["remoteHeadAfterFetch"], remote_head)
        self.assertEqual(result["localHeadBeforeUpdate"], base_master)
        self.assertEqual(result["localHeadAfterUpdate"], remote_head)
        self.assertEqual(git(repo, "rev-parse", "master").stdout.strip(), remote_head)
        recovery_text = json.dumps(result["recoveryCommand"], sort_keys=True)
        self.assertIn("git fetch origin master", recovery_text)
        self.assertIn("work-block-complete.ps1", recovery_text)
        self.assertNotIn("force", recovery_text.lower())
        self.assertIn("target_push_recovery", self.audit_types(repo))

    def test_safe_local_branch_pruning_retains_foreign_dirty_files(self) -> None:
        repo = self.init_repo()
        self.make_feature(repo, "wb-prune")
        start_work_block(repo, work_block_id="wb-prune-foreign-owner", actor="local-test", path_claims=["foreign.txt"])
        (repo / "foreign.txt").write_text("keep my local edit\n", encoding="utf-8")
        self.approve_current_tuple(repo, "wb-prune")
        result = finalize_work_block(repo, work_block_id="wb-prune")
        self.assertEqual(result["status"], "success")
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")
        self.assertEqual((repo / "foreign.txt").read_text(encoding="utf-8"), "keep my local edit\n")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/test-work", check=False).returncode, 0)
        self.assertIn("branch_deletion", self.audit_types(repo))

    def test_repo_sweep_plans_branches_worktrees_and_stashes_without_mutation(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/merged")
        (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
        git(repo, "add", "merged.txt")
        git(repo, "commit", "-m", "merged branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/merged", "-m", "merge codex merged")
        git(repo, "checkout", "-b", "codex/unmerged")
        (repo / "unmerged.txt").write_text("unmerged\n", encoding="utf-8")
        git(repo, "add", "unmerged.txt")
        git(repo, "commit", "-m", "unmerged branch")
        git(repo, "checkout", "master")
        (repo / "README.md").write_text("stashed change\n", encoding="utf-8")
        git(repo, "stash", "push", "-m", "sweep test stash")
        detached = self.tempdir / "detached-worktree"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / "detached.txt").write_text("dirty detached\n", encoding="utf-8")

        result = repo_sweep(repo)
        self.assertEqual(result["status"], "planned")
        branch_dispositions = {item["branch"]: item["disposition"] for item in result["plan"]["branchPlans"]}
        self.assertEqual(branch_dispositions["codex/merged"], "prune_merged_branch")
        self.assertEqual(branch_dispositions["codex/unmerged"], "merge_required")
        self.assertEqual(branch_dispositions["master"], "retain_protected_branch")
        self.assertEqual(result["plan"]["stashPlans"][0]["disposition"], "retain_stash")
        detached_plan = next(item for item in result["plan"]["worktreePlans"] if Path(item.get("path", "")).resolve() == detached.resolve())
        self.assertEqual(detached_plan["disposition"], "retain_dirty_detached_worktree")
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/merged").returncode, 0)

    def test_repo_sweep_apply_auto_quorums_and_prunes_only_merged_branch(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/merged")
        (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
        git(repo, "add", "merged.txt")
        git(repo, "commit", "-m", "merged branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/merged", "-m", "merge codex merged")
        applied = repo_sweep(repo, apply=True)
        self.assertEqual(applied["status"], "success")
        self.assertTrue(applied["quorumResults"][0]["autoGenerated"])
        self.assertTrue(applied["quorumResults"][0]["quorum"]["ok"])
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/merged", check=False).returncode, 0)
        self.assertIn("branch_deletion", self.audit_types(repo))
        packet_path = Path(applied["quorumResults"][0]["unblockDetail"]["reviewPacketPath"])
        self.assertTrue(packet_path.exists())
        self.assertTrue((packet_path.parent / "accepted-review-manifest.json").exists())

    def test_repo_sweep_reuses_one_plan_and_one_dirty_probe_per_worktree(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "single-probe-detached"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        config = load_closeout_config(repo)

        with mock.patch(
            "tools.repo_hygiene.brokered_closeout.worktree_dirty_state",
            wraps=worktree_dirty_state,
        ) as dirty_probe:
            plan = repo_sweep_plan(repo, config)
        self.assertEqual(dirty_probe.call_count, len(plan["worktreePlans"]))

        with mock.patch(
            "tools.repo_hygiene.brokered_closeout.repo_sweep_plan",
            wraps=repo_sweep_plan,
        ) as plan_probe, mock.patch(
            "tools.repo_hygiene.brokered_closeout.load_closeout_config",
            wraps=load_closeout_config,
        ) as config_probe:
            result = repo_sweep(repo)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(plan_probe.call_count, 1)
        self.assertEqual(config_probe.call_count, 1)

        stale_plan = json.loads(json.dumps(plan))
        stale_plan["pinnedRefs"]["target"]["head"] = "0" * 40
        with self.assertRaisesRegex(HygieneError, "plan evidence hash is stale or invalid"):
            repo_sweep_tuple(repo, plan=stale_plan, config=config)

        mismatched_config = json.loads(json.dumps(config))
        mismatched_config["policyHash"] = "0" * 32
        with self.assertRaisesRegex(HygieneError, "plan policy hash does not match"):
            repo_sweep_tuple(repo, plan=plan, config=mismatched_config)

    def test_repo_sweep_investigations_resume_in_bounded_content_addressed_pages(self) -> None:
        repo = self.init_repo(config_updates={"repoSweep": {"maxNewInvestigationsPerRun": 1}})
        branches = ["codex/paged-one", "codex/paged-two", "codex/paged-three"]
        for index, branch in enumerate(branches):
            git(repo, "checkout", "-b", branch, "master")
            path = repo / f"paged-{index}.txt"
            path.write_text(f"page {index}\n", encoding="utf-8")
            git(repo, "add", path.name)
            git(repo, "commit", "-m", f"paged candidate {index}")
        git(repo, "checkout", "master")

        first = repo_sweep(repo)
        self.assertEqual(first["status"], "planned_partial")
        self.assertEqual(first["investigationProgress"]["total"], 3)
        self.assertEqual(first["investigationProgress"]["generated"], 1)
        self.assertEqual(first["investigationProgress"]["reused"], 0)
        self.assertEqual(len(first["investigationProgress"]["pendingCandidateIds"]), 2)

        second = repo_sweep(repo, apply=True)
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["reason"], "repo_sweep_investigations_pending")
        self.assertEqual(second["investigationProgress"]["generated"], 1)
        self.assertEqual(second["investigationProgress"]["reused"], 1)
        self.assertEqual(len(second["investigationProgress"]["pendingCandidateIds"]), 1)
        for branch in branches:
            self.assertEqual(git(repo, "rev-parse", "--verify", branch).returncode, 0)

        third = repo_sweep(repo)
        self.assertEqual(third["status"], "planned")
        self.assertTrue(third["investigationProgress"]["complete"])
        self.assertEqual(third["investigationProgress"]["generated"], 1)
        self.assertEqual(third["investigationProgress"]["reused"], 2)

        fourth = repo_sweep(repo)
        self.assertEqual(fourth["status"], "planned")
        self.assertEqual(fourth["investigationProgress"]["generated"], 0)
        self.assertEqual(fourth["investigationProgress"]["reused"], 3)

    def test_repo_sweep_investigation_cache_rejects_dirty_source_item_drift(self) -> None:
        repo = self.init_repo(config_updates={"repoSweep": {"maxNewInvestigationsPerRun": 1}})
        branch = "codex/dirty-cache-drift"
        git(repo, "checkout", "-b", branch)
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "dirty cache branch")
        first_dirty = repo / "first-untracked.txt"
        first_dirty.write_text("first\n", encoding="utf-8")
        detached = self.tempdir / "dirty-cache-detached"
        git(repo, "worktree", "add", "--detach", str(detached), "master")
        (detached / "detached-untracked.txt").write_text("detached\n", encoding="utf-8")

        first = repo_sweep(repo)
        self.assertEqual(first["status"], "planned_partial")
        self.assertEqual(first["investigationProgress"]["generated"], 1)
        cached = first["investigationProgress"]["reports"][0]
        self.assertEqual(cached["branch"], branch)
        self.assertIn("first-untracked.txt", cached["dirtyClassification"]["dirtyPaths"])

        first_dirty.unlink()
        (repo / "second-untracked.txt").write_text("second\n", encoding="utf-8")
        second = repo_sweep(repo, apply=True)

        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["reason"], "repo_sweep_investigations_pending")
        self.assertEqual(second["investigationProgress"]["generated"], 1)
        self.assertEqual(second["investigationProgress"]["reused"], 0)
        refreshed = second["investigationProgress"]["reports"][0]
        self.assertNotEqual(refreshed["candidateId"], cached["candidateId"])
        self.assertIn("second-untracked.txt", refreshed["dirtyClassification"]["dirtyPaths"])
        self.assertNotIn("first-untracked.txt", refreshed["dirtyClassification"]["dirtyPaths"])

    def test_repo_sweep_cached_recommendation_cannot_be_rehashed_into_prune_authority(self) -> None:
        repo = self.init_repo(config_updates={"repoSweep": {"maxNewInvestigationsPerRun": 1}})
        branch = "codex/cached-recommendation-forgery"
        git(repo, "checkout", "-b", branch, "master")
        (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        git(repo, "add", "candidate.txt")
        git(repo, "commit", "-m", "candidate branch")
        git(repo, "checkout", "master")

        first = repo_sweep(repo)
        self.assertEqual(first["status"], "planned")
        original = next(report for report in first["retainedCandidateReports"] if report.get("branch") == branch)
        self.assertEqual(original["recommendedAction"], "clean_integrate_now")
        report_path = Path(original["reportPath"])
        forged = json.loads(report_path.read_text(encoding="utf-8"))
        forged["recommendedAction"] = "prune_now"
        forged["actionClass"] = "redundant_branch_prune"
        forged["blockers"] = []
        forged["remediationProof"]["recommendedAction"] = "prune_now"
        forged["remediationProof"]["actionClass"] = "redundant_branch_prune"
        forged["remediationProof"]["blockers"] = []
        forged["evidenceHash"] = stable_hash(
            {key: value for key, value in forged.items() if key not in {"evidenceHash", "reportPath"}}
        )
        report_path.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")

        forged_plan = repo_sweep(repo)
        forged_candidate = next(
            candidate
            for candidate in forged_plan["promotedCandidates"]
            if ((candidate.get("pinnedRefs") or {}).get("branch") or {}).get("branch") == branch
        )
        self.assertEqual(forged_candidate["actionId"], "delete_local_branch")

        applied = repo_sweep(repo, apply=True, candidate_id=forged_candidate["candidateId"])
        self.assertEqual(applied["status"], "blocked")
        self.assertEqual(applied["reason"], "candidate_not_found_or_not_promoted")
        self.assertEqual(git(repo, "rev-parse", "--verify", branch).returncode, 0)
        refreshed = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["recommendedAction"], "clean_integrate_now")
        self.assertEqual(refreshed["actionClass"], "repo_sweep_clean_integrate")

    def test_repo_sweep_prune_worktrees_clean_detached_only_removes_clean_detached(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "clean-detached-worktree"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")

        applied = repo_sweep(repo, apply=True)

        self.assertEqual(applied["status"], "success")
        self.assertFalse(detached.exists())
        self.assertTrue(any(item.get("action") == "remove_clean_detached_worktree" for item in applied["actions"]))
        self.assertIn("snapshot_pruning", self.audit_types(repo))

    def test_repo_sweep_manual_only_candidate_reports_recoverable_unblock_detail(self) -> None:
        repo = self.init_repo(config_updates={"autoQuorum": {"autonomousActionClasses": [], "manualOnlyActionClasses": ["integrated_branch_prune"]}})
        git(repo, "checkout", "-b", "codex/merged")
        (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
        git(repo, "add", "merged.txt")
        git(repo, "commit", "-m", "merged branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/merged", "-m", "merge codex merged")
        result = repo_sweep(repo, apply=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "review_quorum_blocked")
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/merged").returncode, 0)
        quorum_result = result["quorumResults"][0]
        self.assertFalse(quorum_result["autoGenerated"])
        self.assertFalse(quorum_result["quorum"]["ok"])
        detail = quorum_result["unblockDetail"]
        self.assertEqual(detail["candidateId"], result["branchCandidates"][0]["candidateId"])
        self.assertEqual(detail["actionId"], "delete_local_branch")
        self.assertIn("policyHash", detail)
        self.assertIn("pinnedRefs", detail)
        self.assertIn("reviewPacketPath", detail)
        self.assertFalse(detail["autoUnblockAllowed"])

    def test_repo_sweep_merge_required_branch_writes_investigation_packet(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/unmerged")
        (repo / "unmerged.txt").write_text("unmerged\n", encoding="utf-8")
        git(repo, "add", "unmerged.txt")
        git(repo, "commit", "-m", "unmerged branch")
        git(repo, "checkout", "master")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/unmerged")
        self.assertEqual(report["sourceDisposition"], "merge_required")
        self.assertEqual(report["recommendedAction"], "clean_integrate_now")
        self.assertTrue(Path(report["reportPath"]).exists())
        self.assertTrue(report["scope"]["mergeProbe"]["clean"])
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/unmerged").returncode, 0)
        self.assertNotEqual(git(repo, "rev-parse", "master").stdout.strip(), git(repo, "rev-parse", "codex/unmerged").stdout.strip())

    def test_repo_sweep_clean_checked_out_branch_can_still_integrate(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/checked")
        (repo / "checked.txt").write_text("checked work\n", encoding="utf-8")
        git(repo, "add", "checked.txt")
        git(repo, "commit", "-m", "checked branch")
        git(repo, "checkout", "master")
        checked_worktree = self.tempdir / "checked-worktree"
        git(repo, "worktree", "add", str(checked_worktree), "codex/checked")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/checked")
        self.assertEqual(report["recommendedAction"], "clean_integrate_now")
        self.assertEqual(git(repo, "show", "master:checked.txt").stdout, "checked work\n")
        self.assertFalse(checked_worktree.exists())
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/checked", check=False).returncode, 0)

    def test_repo_sweep_stale_locked_clean_worktree_can_be_cleaned(self) -> None:
        repo = self.init_repo(config_updates={"repoSweep": {"lockedWorktreeStaleHours": 0}})
        git(repo, "checkout", "-b", "codex/stale-lock")
        (repo / "stale.txt").write_text("stale lock work\n", encoding="utf-8")
        git(repo, "add", "stale.txt")
        git(repo, "commit", "-m", "stale lock branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/stale-lock", "-m", "merge stale lock")
        locked_worktree = self.tempdir / "locked-worktree"
        git(repo, "worktree", "add", str(locked_worktree), "codex/stale-lock")
        git(repo, "worktree", "lock", "--reason", "pid=999999 stale test lock", str(locked_worktree))
        lock_file = Path(git(locked_worktree, "rev-parse", "--git-dir").stdout.strip()) / "locked"
        if not lock_file.is_absolute():
            lock_file = locked_worktree / lock_file
        old_time = 1_600_000_000
        os.utime(lock_file, (old_time, old_time))

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/stale-lock")
        self.assertEqual(report["recommendedAction"], "cleanup_worktree_and_prune")
        self.assertEqual(report["actionClass"], "stale_locked_worktree_cleanup")
        self.assertFalse(report["lockInspection"]["pidAlive"])
        self.assertFalse(locked_worktree.exists())
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/stale-lock", check=False).returncode, 0)

    def test_repo_sweep_backup_branch_is_analyzed_before_prune(self) -> None:
        repo = self.init_repo()
        git(repo, "branch", "codex/master-backup", "master")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/master-backup")
        self.assertTrue(report["backupAnalysis"]["isBackupBranch"])
        self.assertEqual(report["backupAnalysis"]["redundantWith"]["kind"], "target")
        self.assertEqual(report["recommendedAction"], "prune_now")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/master-backup", check=False).returncode, 0)

    def test_repo_sweep_patch_equivalent_backup_branch_is_pruned(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/topic-backup")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "backup duplicate work")
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "integrated duplicate work")
        (repo / "target-only.txt").write_text("target-only\n", encoding="utf-8")
        git(repo, "add", "target-only.txt")
        git(repo, "commit", "-m", "target-only follow-up")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/topic-backup")
        self.assertEqual(report["backupAnalysis"]["redundantWith"]["kind"], "target_patch_equivalent")
        self.assertEqual(report["recommendedAction"], "prune_now")
        deletion = next(item for item in result["actions"] if item.get("branch") == "codex/topic-backup" and item.get("action") == "delete_local_branch")
        self.assertTrue(deletion["forced"])
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/topic-backup", check=False).returncode, 0)

    def test_repo_sweep_patch_equivalent_non_backup_branch_auto_quorum_prunes(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/topic-copy")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "copy duplicate work")
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "integrated duplicate work")
        (repo / "target-only.txt").write_text("target-only\n", encoding="utf-8")
        git(repo, "add", "target-only.txt")
        git(repo, "commit", "-m", "target-only follow-up")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/topic-copy")
        self.assertFalse(report["backupAnalysis"]["isBackupBranch"])
        self.assertEqual(report["backupAnalysis"]["redundantWith"]["kind"], "target_patch_equivalent")
        self.assertEqual(report["actionClass"], "redundant_branch_prune")
        quorum = next(item for item in result["quorumResults"] if item["report"]["branch"] == "codex/topic-copy")
        self.assertTrue(quorum["autoGenerated"])
        self.assertTrue(quorum["quorum"]["ok"])
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/topic-copy", check=False).returncode, 0)

    def test_non_ancestor_historical_branch_prune_requires_bundle_backed_recovery(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/historical-copy")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "historical duplicate work")
        branch_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "integrated duplicate work")
        (repo / "target-only.txt").write_text("target-only\n", encoding="utf-8")
        git(repo, "add", "target-only.txt")
        git(repo, "commit", "-m", "target-only follow-up")
        target_head = git(repo, "rev-parse", "HEAD").stdout.strip()

        result = repo_sweep(repo, apply=True)

        deletion = next(item for item in result["actions"] if item.get("branch") == "codex/historical-copy" and item.get("action") == "delete_local_branch")
        recovery = deletion["recoveryArtifact"]
        bundle = recovery["bundle"]
        self.assertEqual(recovery["branchHead"], branch_head)
        self.assertEqual(recovery["targetHead"], target_head)
        self.assertTrue(bundle["required"])
        bundle_path = repo / bundle["path"]
        self.assertTrue(bundle_path.exists(), recovery)
        self.assertEqual(bundle["sha256"], __import__("tools.repo_hygiene.brokered_closeout", fromlist=["file_content_hash"]).file_content_hash(bundle_path))
        self.assertEqual(git(repo, "bundle", "verify", str(bundle_path)).returncode, 0)
        quorum = next(item for item in result["quorumResults"] if item["report"]["branch"] == "codex/historical-copy")
        self.assertEqual(quorum["candidate"]["evidence"]["recoveryArtifact"]["artifactPath"], recovery["artifactPath"])
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/historical-copy", check=False).returncode, 0)

    def test_recovery_audit_records_heads_hashes_and_reviewer_verdicts(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "dirty-detached-recovery-audit"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        worktree_head = git(detached, "rev-parse", "HEAD").stdout.strip()
        (detached / "README.md").write_text("tracked dirty bytes\n", encoding="utf-8")
        (detached / "untracked.bin").write_bytes(b"\x00dirty-bytes\n")

        result = repo_sweep(repo, apply=True)

        action = next(item for item in result["actions"] if item.get("action") == "detached_dirty_preserve")
        recovery = action["dirtyRecovery"]
        artifact = json.loads((repo / recovery["artifactPath"]).read_text(encoding="utf-8"))
        self.assertEqual(artifact["worktreeHead"], worktree_head)
        self.assertEqual(artifact["preservationHead"], action["preservationHead"])
        self.assertEqual(artifact["targetHead"], result["plan"]["pinnedRefs"]["target"]["head"])
        self.assertTrue(artifact["fileHashes"])
        self.assertGreaterEqual(len(artifact["reviewerVerdicts"]), 3)
        untracked = next(item for item in artifact["untrackedBytes"] if item["path"] == "untracked.bin")
        self.assertEqual(untracked["byteSha256"], __import__("tools.repo_hygiene.brokered_closeout", fromlist=["file_content_hash"]).file_content_hash(repo / untracked["preservedPath"]))
        self.assertIn("manual_prune_recovery", self.audit_types(repo))

    def test_stale_transaction_branch_pruned_after_recovery_evidence(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/archive-only")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "archive only duplicate")
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "target carries duplicate")
        (repo / "followup.txt").write_text("target followup\n", encoding="utf-8")
        git(repo, "add", "followup.txt")
        git(repo, "commit", "-m", "target followup")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/archive-only")
        self.assertEqual(report["recommendedAction"], "prune_now")
        deletion = next(item for item in result["actions"] if item.get("branch") == "codex/archive-only" and item.get("action") == "delete_local_branch")
        self.assertTrue((repo / deletion["recoveryArtifact"]["artifactPath"]).exists())
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/archive-only", check=False).returncode, 0)

    def test_final_repo_sweep_after_prune_reports_zero_candidates(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/prune-fixed-point")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "duplicate")
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "duplicate integrated")
        (repo / "target-only.txt").write_text("target-only\n", encoding="utf-8")
        git(repo, "add", "target-only.txt")
        git(repo, "commit", "-m", "target-only")

        result = repo_sweep(repo, apply=True)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["postPruneSweep"]["branchCandidateCount"], 0)
        self.assertEqual(result["postPruneSweep"]["remoteFeatureCandidateCount"], 0)
        self.assertEqual(result["postPruneSweep"]["stashCandidateCount"], 0)

    def test_deletion_tuple_rejects_missing_or_stale_recovery_artifact(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/missing-recovery")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "duplicate")
        git(repo, "checkout", "master")
        (repo / "dup.txt").write_text("same patch\n", encoding="utf-8")
        git(repo, "add", "dup.txt")
        git(repo, "commit", "-m", "duplicate integrated")
        (repo / "target-only.txt").write_text("target-only\n", encoding="utf-8")
        git(repo, "add", "target-only.txt")
        git(repo, "commit", "-m", "target-only")

        def fake_recovery(*args: object, **kwargs: object) -> dict:
            return {
                "status": "success",
                "candidateId": kwargs["candidate_id"],
                "artifactPath": ".claude-state/closeout/manual-prune/missing/recovery.json",
                "branchHead": kwargs["head"],
                "targetHead": kwargs["target_head"],
                "evidenceHash": "missing-artifact",
                "bundle": {"required": True, "path": ".claude-state/closeout/manual-prune/missing/recovery.bundle", "sha256": "missing"},
                "reviewerVerdicts": [{"reviewer": "codex-self", "score": 10, "blockers": []}],
            }

        with mock.patch("tools.repo_hygiene.brokered_closeout.write_evidence_preserving_prune_recovery", side_effect=fake_recovery):
            result = repo_sweep(repo, apply=True)

        action = next(item for item in result["actions"] if item.get("action") == "retain_prune_recovery_artifact_invalid")
        self.assertIn("recovery_artifact_missing", action["recoveryVerification"]["reasons"])
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/missing-recovery").returncode, 0)

    def test_repo_sweep_apply_without_candidate_blocks_multiple_candidates(self) -> None:
        repo = self.init_repo()
        git(repo, "branch", "codex/first-backup", "master")
        git(repo, "branch", "codex/second-backup", "master")

        result = repo_sweep(repo, apply=True)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "repo_sweep_bulk_override_required")
        self.assertEqual(result["applyScope"]["candidateCount"], 2)
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/first-backup").returncode, 0)
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/second-backup").returncode, 0)
        self.assertIn("repo_sweep_bulk_override_required", self.audit_types(repo))

    def test_repo_sweep_audited_bulk_override_allows_exact_candidate_set(self) -> None:
        repo = self.init_repo(config_updates={"repoSweep": {"auditedBulkOverride": {"enabled": True}}})
        git(repo, "branch", "codex/first-backup", "master")
        git(repo, "branch", "codex/second-backup", "master")
        plan = repo_sweep(repo)
        override = {
            "enabled": True,
            "reason": "test bulk override for exact redundant backup branches",
            "approvedBy": "codex-test",
            "candidateIds": plan["applyScope"]["candidateIds"],
            "perCandidateTuples": plan["applyScope"]["candidateTuples"],
            "reviewerApproval": {
                "approved": True,
                "reviewer": "codex-test-reviewer",
            },
            "recoveryCommand": "Restore the backup branches from the evidence-preserving prune artifacts.",
        }

        result = repo_sweep(repo, apply=True, bulk_override=override)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["applyScope"]["candidateCount"], 2)
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/first-backup", check=False).returncode, 0)
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/second-backup", check=False).returncode, 0)
        self.assertIn("repo_sweep_audited_bulk_override", self.audit_types(repo))

    def test_repo_sweep_apply_candidate_id_mutates_only_that_candidate(self) -> None:
        repo = self.init_repo()
        git(repo, "branch", "codex/first-backup", "master")
        git(repo, "branch", "codex/second-backup", "master")
        plan = repo_sweep(repo)
        candidate = next(
            item
            for item in plan["promotedCandidates"]
            if item["pinnedRefs"]["branch"]["branch"] == "codex/first-backup"
        )

        result = repo_sweep(repo, apply=True, candidate_id=candidate["candidateId"])

        self.assertEqual(result["status"], "success", result)
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/first-backup", check=False).returncode, 0)
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/second-backup", check=False).returncode, 0)

    def test_repo_sweep_remote_integrated_feature_branch_is_pruned(self) -> None:
        repo = self.init_repo(remote=True)
        git(repo, "checkout", "-b", "codex/remote-integrated")
        (repo / "remote-integrated.txt").write_text("remote integrated\n", encoding="utf-8")
        git(repo, "add", "remote-integrated.txt")
        git(repo, "commit", "-m", "remote integrated branch")
        git(repo, "push", "origin", "codex/remote-integrated")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/remote-integrated", "-m", "merge remote integrated")
        git(repo, "push", "origin", "master")
        git(repo, "branch", "-D", "codex/remote-integrated")

        result = repo_sweep(repo, apply=True)

        self.assertEqual(result["status"], "success", result)
        candidate = next(item for item in result["remoteFeatureCandidates"] if item["pinnedRefs"]["remoteFeature"]["branch"] == "codex/remote-integrated")
        self.assertEqual(candidate["actionId"], "delete_remote_branch")
        self.assertTrue(any(item.get("action") == "delete_remote_branch" and item.get("branch") == "codex/remote-integrated" for item in result["actions"]))
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", "codex/remote-integrated").stdout.strip(), "")
        self.assertIn("remote_branch_deletion", self.audit_types(repo))

    def test_remote_feature_delete_is_idempotent_when_provider_already_removed_ref(self) -> None:
        repo = self.init_repo(remote=True)
        git(repo, "checkout", "-b", "codex/provider-already-deleted")
        (repo / "provider-deleted.txt").write_text("provider deleted\n", encoding="utf-8")
        git(repo, "add", "provider-deleted.txt")
        git(repo, "commit", "-m", "provider deleted branch")
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", "codex/provider-already-deleted")
        remote_path = Path(git(repo, "remote", "get-url", "origin").stdout.strip())
        git(remote_path, "update-ref", "-d", "refs/heads/codex/provider-already-deleted")

        result = delete_remote_feature_ref(
            repo,
            load_closeout_config(repo),
            remote="origin",
            branch="codex/provider-already-deleted",
            expected_head=head,
        )

        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["alreadyMissing"])
        self.assertEqual(result["providerQueryBefore"]["status"], "absent")
        self.assertEqual(result["trackingCleanupReturncode"], 0)
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", "codex/provider-already-deleted").stdout.strip(), "")

    def test_remote_feature_delete_fetches_when_tracking_ref_is_missing(self) -> None:
        repo = self.init_repo(remote=True)
        branch = "codex/missing-tracking-only"
        git(repo, "checkout", "-b", branch)
        (repo / "missing-tracking.txt").write_text("still remote\n", encoding="utf-8")
        git(repo, "add", "missing-tracking.txt")
        git(repo, "commit", "-m", "remote branch with missing tracking ref")
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", branch)
        git(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")

        result = delete_remote_feature_ref(
            repo,
            load_closeout_config(repo),
            remote="origin",
            branch=branch,
            expected_head=head,
        )

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["returncode"], 0)
        self.assertIsNone(result["remoteTrackingHeadAfter"])
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", branch).stdout.strip(), "")

    def test_remote_feature_delete_ignores_narrow_fetch_refspec_for_provider_truth(self) -> None:
        repo = self.init_repo(remote=True)
        branch = "codex/narrow-refspec"
        git(repo, "checkout", "-b", branch)
        (repo / "narrow.txt").write_text("provider truth\n", encoding="utf-8")
        git(repo, "add", "narrow.txt")
        git(repo, "commit", "-m", "narrow refspec branch")
        expected_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", branch)
        git(repo, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/master:refs/remotes/origin/master")
        git(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")

        result = delete_remote_feature_ref(
            repo,
            load_closeout_config(repo),
            remote="origin",
            branch=branch,
            expected_head=expected_head,
        )

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["providerQueryBefore"]["head"], expected_head)
        self.assertEqual(result["providerQueryAfter"]["status"], "absent")
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", branch).stdout.strip(), "")

    def test_remote_feature_delete_lease_rejects_concurrent_advance(self) -> None:
        repo = self.init_repo(remote=True)
        branch = "codex/concurrent-advance"
        git(repo, "checkout", "-b", branch)
        (repo / "advance.txt").write_text("old\n", encoding="utf-8")
        git(repo, "add", "advance.txt")
        git(repo, "commit", "-m", "old remote head")
        expected_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", branch)
        (repo / "advance.txt").write_text("new\n", encoding="utf-8")
        git(repo, "commit", "-am", "new remote head")
        advanced_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", f"HEAD:refs/heads/race-source")
        remote_path = Path(git(repo, "remote", "get-url", "origin").stdout.strip())
        real_run_git = run_git
        advanced = False

        def race_before_delete(root: Path, args: list[str], **kwargs: object):
            nonlocal advanced
            if not advanced and args and args[0] == "push" and any(str(arg).startswith("--force-with-lease=") for arg in args):
                git(remote_path, "update-ref", f"refs/heads/{branch}", advanced_head)
                advanced = True
            return real_run_git(root, args, **kwargs)

        with mock.patch("tools.repo_hygiene.brokered_closeout.run_git", side_effect=race_before_delete):
            result = delete_remote_feature_ref(
                repo,
                load_closeout_config(repo),
                remote="origin",
                branch=branch,
                expected_head=expected_head,
            )

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "remote_feature_still_present_after_delete")
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["providerQueryAfter"]["head"], advanced_head)
        self.assertIn(advanced_head, git(repo, "ls-remote", "--heads", "origin", branch).stdout)

    def test_remote_feature_delete_blocks_ref_recreated_after_delete(self) -> None:
        repo = self.init_repo(remote=True)
        branch = "codex/recreated-after-delete"
        git(repo, "checkout", "-b", branch)
        (repo / "recreate.txt").write_text("old\n", encoding="utf-8")
        git(repo, "add", "recreate.txt")
        git(repo, "commit", "-m", "old recreated head")
        expected_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", branch)
        (repo / "recreate.txt").write_text("new\n", encoding="utf-8")
        git(repo, "commit", "-am", "new recreated head")
        recreated_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", f"HEAD:refs/heads/race-source")
        remote_path = Path(git(repo, "remote", "get-url", "origin").stdout.strip())
        real_run_git = run_git
        query_count = 0

        def recreate_before_postfetch(root: Path, args: list[str], **kwargs: object):
            nonlocal query_count
            if args and args[0] == "ls-remote":
                query_count += 1
                if query_count == 2:
                    git(remote_path, "update-ref", f"refs/heads/{branch}", recreated_head)
            return real_run_git(root, args, **kwargs)

        with mock.patch("tools.repo_hygiene.brokered_closeout.run_git", side_effect=recreate_before_postfetch):
            result = delete_remote_feature_ref(
                repo,
                load_closeout_config(repo),
                remote="origin",
                branch=branch,
                expected_head=expected_head,
            )

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["reason"], "remote_feature_still_present_after_delete")
        self.assertEqual(result["providerQueryAfter"]["head"], recreated_head)
        self.assertIn(recreated_head, git(repo, "ls-remote", "--heads", "origin", branch).stdout)

    def test_remote_feature_delete_blocks_when_postdelete_exact_query_fails(self) -> None:
        repo = self.init_repo(remote=True)
        branch = "codex/postdelete-fetch-failure"
        git(repo, "checkout", "-b", branch)
        (repo / "postfetch.txt").write_text("postfetch\n", encoding="utf-8")
        git(repo, "add", "postfetch.txt")
        git(repo, "commit", "-m", "postdelete fetch failure")
        expected_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "push", "origin", branch)
        real_run_git = run_git
        query_count = 0

        def fail_postdelete_fetch(root: Path, args: list[str], **kwargs: object):
            nonlocal query_count
            if args and args[0] == "ls-remote":
                query_count += 1
                if query_count == 2:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="simulated postdelete exact-query failure")
            return real_run_git(root, args, **kwargs)

        with mock.patch("tools.repo_hygiene.brokered_closeout.run_git", side_effect=fail_postdelete_fetch):
            result = delete_remote_feature_ref(
                repo,
                load_closeout_config(repo),
                remote="origin",
                branch=branch,
                expected_head=expected_head,
            )

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "remote_feature_exact_query_failed")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["providerQueryAfter"]["returncode"], 1)

    def test_repo_sweep_remote_patch_equivalent_feature_branch_is_pruned(self) -> None:
        repo = self.init_repo(remote=True)
        git(repo, "checkout", "-b", "codex/remote-copy")
        (repo / "dup-remote.txt").write_text("same remote patch\n", encoding="utf-8")
        git(repo, "add", "dup-remote.txt")
        git(repo, "commit", "-m", "remote duplicate patch")
        git(repo, "push", "origin", "codex/remote-copy")
        git(repo, "checkout", "master")
        (repo / "dup-remote.txt").write_text("same remote patch\n", encoding="utf-8")
        git(repo, "add", "dup-remote.txt")
        git(repo, "commit", "-m", "target duplicate patch")
        (repo / "target-after-dup.txt").write_text("target follow-up\n", encoding="utf-8")
        git(repo, "add", "target-after-dup.txt")
        git(repo, "commit", "-m", "target follow-up")
        git(repo, "push", "origin", "master")
        git(repo, "branch", "-D", "codex/remote-copy")

        result = repo_sweep(repo, apply=True)

        candidate = next(item for item in result["remoteFeatureCandidates"] if item["pinnedRefs"]["remoteFeature"]["branch"] == "codex/remote-copy")
        self.assertEqual(candidate["actionClass"], "patch_equivalent_remote_feature_prune")
        quorum = next(item for item in result["quorumResults"] if item["candidate"]["candidateId"] == candidate["candidateId"])
        self.assertTrue(quorum["autoGenerated"])
        self.assertTrue(quorum["quorum"]["ok"])
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", "codex/remote-copy").stdout.strip(), "")

    def test_repo_sweep_remote_unique_feature_branch_clean_integrates_and_prunes(self) -> None:
        repo = self.init_repo(remote=True)
        git(repo, "checkout", "-b", "codex/remote-unique")
        (repo / "remote-unique.txt").write_text("remote unique work\n", encoding="utf-8")
        git(repo, "add", "remote-unique.txt")
        git(repo, "commit", "-m", "remote unique branch")
        git(repo, "push", "origin", "codex/remote-unique")
        git(repo, "checkout", "master")
        git(repo, "branch", "-D", "codex/remote-unique")

        result = remediate_retained_candidates(repo, apply=True)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["actor"], "retained-remediation")
        report = next(item for item in result["retainedCandidateReports"] if item.get("remoteRef") == "origin/codex/remote-unique")
        self.assertEqual(report["recommendedAction"], "clean_integrate_remote_now")
        self.assertTrue(any(item.get("action") == "clean_integrate_remote_feature" and item.get("branch") == "codex/remote-unique" for item in result["actions"]))
        self.assertEqual(git(repo, "show", "master:remote-unique.txt").stdout, "remote unique work\n")
        self.assertEqual(git(repo, "ls-remote", "--heads", "origin", "codex/remote-unique").stdout.strip(), "")

    def test_repo_sweep_remote_conflicting_feature_branch_writes_investigation_packet(self) -> None:
        repo = self.init_repo(remote=True)
        (repo / "remote-conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "remote-conflict.txt")
        git(repo, "commit", "-m", "remote conflict base")
        git(repo, "push", "origin", "master")
        git(repo, "checkout", "-b", "codex/remote-conflict")
        (repo / "remote-conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "remote-conflict.txt")
        git(repo, "commit", "-m", "remote conflict branch")
        git(repo, "push", "origin", "codex/remote-conflict")
        git(repo, "checkout", "master")
        (repo / "remote-conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "remote-conflict.txt")
        git(repo, "commit", "-m", "remote conflict target")
        git(repo, "push", "origin", "master")
        git(repo, "branch", "-D", "codex/remote-conflict")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item.get("remoteRef") == "origin/codex/remote-conflict")
        self.assertEqual(report["reportType"], "repo_sweep_remote_feature_investigation")
        self.assertEqual(report["recommendedAction"], "dispatch_conflict_remediation")
        self.assertEqual(report["actionClass"], "agent_conflict_remediation")
        self.assertEqual(report["scope"]["mergeProbe"]["reason"], "merge_failed")
        self.assertEqual(report["scope"]["agentResolutionPacket"]["symbolicAction"], "resolve-conflicts-with-agent")

    def test_remediate_retained_actor_applies_one_candidate_per_run(self) -> None:
        repo = self.init_repo(remote=True)
        for name in ("codex/remote-one", "codex/remote-two"):
            git(repo, "checkout", "-b", name)
            (repo / ("%s.txt" % name.replace("/", "-"))).write_text("%s\n" % name, encoding="utf-8")
            git(repo, "add", ("%s.txt" % name.replace("/", "-")))
            git(repo, "commit", "-m", "work %s" % name)
            git(repo, "push", "origin", name)
            git(repo, "checkout", "master")
            git(repo, "merge", "--no-ff", name, "-m", "merge %s" % name)
            git(repo, "push", "origin", "master")
            git(repo, "branch", "-D", name)

        result = remediate_retained_candidates(repo, apply=True)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["plannedCandidateCount"], 2)
        remaining = git(repo, "ls-remote", "--heads", "origin", "codex/remote-one", "codex/remote-two").stdout.strip().splitlines()
        self.assertEqual(len([line for line in remaining if line.strip()]), 1)

    def test_repo_sweep_dirty_current_worktree_gets_ownership_classification(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/dirty-owned")
        start_work_block(repo, work_block_id="wb-dirty-owned", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned base\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned branch")
        (repo / "owned.txt").write_text("owned dirty\n", encoding="utf-8")
        (repo / "foreign-dirty.txt").write_text("foreign dirty\n", encoding="utf-8")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/dirty-owned")
        self.assertEqual(report["sourceDisposition"], "retain_dirty_worktree")
        self.assertEqual(report["recommendedAction"], "split_now")
        self.assertEqual(report["actionClass"], "dirty_split")
        classification = report["dirtyClassification"]
        self.assertEqual(classification["workBlockId"], "wb-dirty-owned")
        self.assertEqual([item["path"] for item in classification["ownedDirty"]], ["owned.txt"])
        self.assertEqual([item["path"] for item in classification["foreignDirty"]], ["foreign-dirty.txt"])

    def test_repo_sweep_baseline_dirty_changed_is_not_pruned_as_foreign_dirty(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/baseline-dirty-integrated")
        (repo / "baseline-dirty.cpp").write_text("dirty before broker\n", encoding="utf-8")
        start_work_block(repo, work_block_id="wb-baseline-dirty-integrated", actor="local-test")
        (repo / "integrated.txt").write_text("integrated branch work\n", encoding="utf-8")
        git(repo, "add", "integrated.txt")
        git(repo, "commit", "-m", "integrated branch work")
        git(repo, "branch", "-f", "master", "HEAD")
        (repo / "baseline-dirty.cpp").write_text("dirty changed during block\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/baseline-dirty-integrated")
        self.assertEqual(report["recommendedAction"], "retain_with_proven_blocker")
        self.assertEqual(report["actionClass"], "dirty_worktree")
        self.assertEqual(report["blockers"], ["baseline-dirty-overlaps-candidate"])
        classification = report["dirtyClassification"]
        self.assertEqual([item["path"] for item in classification["mixedDirty"]], ["baseline-dirty.cpp"])
        self.assertTrue(classification["mixedDirty"][0]["dirtyBaselineContentChanged"])
        self.assertFalse(classification["foreignDirty"], classification)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "codex/baseline-dirty-integrated")
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/baseline-dirty-integrated").returncode, 0)

    def test_repo_sweep_foreign_dirty_integrated_branch_switches_and_prunes(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/foreign-integrated")
        (repo / "integrated.txt").write_text("integrated\n", encoding="utf-8")
        git(repo, "add", "integrated.txt")
        git(repo, "commit", "-m", "integrated branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/foreign-integrated", "-m", "merge integrated branch")
        git(repo, "checkout", "codex/foreign-integrated")
        (repo / "foreign-local.txt").write_text("foreign dirty stays\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/foreign-integrated")
        self.assertEqual(report["recommendedAction"], "switch_target_and_prune")
        self.assertEqual(report["actionClass"], "foreign_dirty_integrated_branch_prune")
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")
        self.assertEqual((repo / "foreign-local.txt").read_text(encoding="utf-8"), "foreign dirty stays\n")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/foreign-integrated", check=False).returncode, 0)

    def test_repo_sweep_foreign_dirty_integrated_branch_detaches_linked_worktree_and_prunes(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/foreign-detached")
        (repo / "integrated.txt").write_text("integrated\n", encoding="utf-8")
        git(repo, "add", "integrated.txt")
        git(repo, "commit", "-m", "integrated branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/foreign-detached", "-m", "merge integrated branch")
        linked = self.tempdir / "foreign-detached-worktree"
        git(repo, "worktree", "add", str(linked), "codex/foreign-detached")
        (linked / "foreign-local.txt").write_text("foreign dirty stays\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/foreign-detached")
        self.assertEqual(report["recommendedAction"], "switch_target_and_prune")
        action = next(item for item in result["actions"] if item.get("action") == "foreign_dirty_integrated_branch_prune")
        self.assertEqual(action["status"], "success", action)
        self.assertEqual(action["switch"]["action"], "detach_foreign_dirty_worktree_at_target")
        self.assertEqual(git(linked, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "HEAD")
        self.assertEqual((linked / "foreign-local.txt").read_text(encoding="utf-8"), "foreign dirty stays\n")
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/foreign-detached", check=False).returncode, 0)

    def test_repo_sweep_foreign_dirty_target_overlap_blocks_with_exact_path_evidence(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/foreign-overlap")
        (repo / "integrated.txt").write_text("integrated\n", encoding="utf-8")
        git(repo, "add", "integrated.txt")
        git(repo, "commit", "-m", "integrated branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/foreign-overlap", "-m", "merge integrated branch")
        (repo / "target-overlap.txt").write_text("target-side change\n", encoding="utf-8")
        git(repo, "add", "target-overlap.txt")
        git(repo, "commit", "-m", "target overlap path")
        linked = self.tempdir / "foreign-overlap-worktree"
        git(repo, "worktree", "add", str(linked), "codex/foreign-overlap")
        (linked / "target-overlap.txt").write_text("foreign dirty overlap\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/foreign-overlap")
        self.assertEqual(report["recommendedAction"], "retain_with_proven_blocker")
        self.assertEqual(report["blockers"], ["foreign_dirty_target_overlap"])
        self.assertEqual(report["scope"]["dirtyTargetDeltaOverlap"], ["target-overlap.txt"])
        self.assertEqual(report["dirtyClassification"]["dirtyPaths"], ["target-overlap.txt"])
        self.assertEqual(report["remediationProof"]["excludedByExactPolicy"], ["foreign_dirty_target_overlap"])
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/foreign-overlap").returncode, 0)

    def test_repo_sweep_detached_dirty_worktree_is_preserved_before_cleanup(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "dirty-detached-preserve"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / "README.md").write_text("detached dirty readme\n", encoding="utf-8")
        (detached / "detached-only.txt").write_text("another exact dirty path\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["sourceDisposition"] == "retain_dirty_detached_worktree")
        self.assertEqual(report["recommendedAction"], "preserve_detached_dirty_now")
        self.assertEqual(report["preservationBranch"].split("/")[:3], ["closeout", "recovery", "detached"])
        action = next(item for item in result["actions"] if item.get("action") == "detached_dirty_preserve")
        self.assertEqual(action["status"], "success", action)
        self.assertFalse(detached.exists())
        self.assertEqual(action["dirtyRecovery"]["status"], "success")
        dirty_artifact = json.loads((repo / action["dirtyRecovery"]["artifactPath"]).read_text(encoding="utf-8"))
        self.assertIn("trackedDiffPath", dirty_artifact)
        self.assertTrue(dirty_artifact["fileHashes"])
        self.assertEqual(sorted(item["path"] for item in action["copied"]), ["README.md", "detached-only.txt"])
        self.assertEqual(git(repo, "show", f"{action['preservationBranch']}:README.md").stdout, "detached dirty readme\n")
        self.assertEqual(git(repo, "show", f"{action['preservationBranch']}:detached-only.txt").stdout, "another exact dirty path\n")
        self.assertIn("orphan_quarantine", self.audit_types(repo))

    def test_repo_sweep_generated_only_detached_dirty_worktree_is_pruned_with_proof(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "generated-only-detached"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / ".qmake.stash").write_text("qmake state\n", encoding="utf-8")
        (detached / "Makefile.Debug").write_text("make state\n", encoding="utf-8")
        (detached / "moc").mkdir()
        (detached / "moc" / "moc_MainWindow.cpp").write_text("moc state\n", encoding="utf-8")
        (detached / "ui").mkdir()
        (detached / "ui" / "ui_MainWindow.h").write_text("ui state\n", encoding="utf-8")
        (detached / "MLVApp_resource.rc").write_text("rc state\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["sourceDisposition"] == "retain_dirty_detached_worktree")
        self.assertEqual(report["recommendedAction"], "prune_generated_detached_dirty_now")
        self.assertEqual(report["actionClass"], "generated_only_detached_worktree_prune")
        proof = report["dirtyClassification"]["generatedOnlyProof"]
        self.assertTrue(proof["eligible"])
        self.assertEqual(proof["unmatched"], [])
        self.assertEqual(proof["pathCount"], 5)
        action = next(item for item in result["actions"] if item.get("action") == "generated_only_detached_dirty_prune")
        self.assertEqual(action["status"], "success", action)
        self.assertTrue(action["generatedOnlyProof"]["eligible"])
        self.assertFalse(detached.exists())

    def test_repo_sweep_large_real_detached_evidence_is_preserved_not_count_blocked(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "large-real-evidence-detached"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        evidence_root = detached / "a5_packet"
        evidence_root.mkdir()
        for index in range(30):
            (evidence_root / f"frame-{index:02d}.json").write_text(json.dumps({"index": index}) + "\n", encoding="utf-8")

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["sourceDisposition"] == "retain_dirty_detached_worktree")
        self.assertEqual(report["recommendedAction"], "preserve_detached_dirty_now")
        self.assertNotIn("too_many_dirty_paths", report["blockers"])
        self.assertFalse(report["dirtyClassification"]["generatedOnlyProof"]["eligible"])
        action = next(item for item in result["actions"] if item.get("action") == "detached_dirty_preserve")
        self.assertEqual(action["status"], "success", action)
        self.assertFalse(detached.exists())
        self.assertEqual(git(repo, "show", f"{action['preservationBranch']}:a5_packet/frame-00.json").stdout, '{"index": 0}\n')
        self.assertEqual(git(repo, "show", f"{action['preservationBranch']}:a5_packet/frame-29.json").stdout, '{"index": 29}\n')

    @unittest.skipUnless(os.name == "nt", "extended-length path prefixes are Windows-specific")
    def test_detached_dirty_preserve_copy_uses_extended_length_destination(self) -> None:
        repo = self.init_repo()
        (repo / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        long_component = "x" * 90
        dest_root = self.tempdir / long_component / long_component / long_component
        self.assertGreater(len(str((dest_root / "evidence.txt").resolve(strict=False))), 260)

        with mock.patch("tools.repo_hygiene.brokered_closeout.os.makedirs") as makedirs, mock.patch(
            "tools.repo_hygiene.brokered_closeout.shutil.copy2"
        ) as copy2:
            copied = copy_exact_path_for_split(repo, dest_root, "evidence.txt", "??")

        self.assertEqual(copied, {"path": "evidence.txt", "operation": "copy_file"})
        self.assertTrue(str(makedirs.call_args.args[0]).startswith("\\\\?\\"))
        self.assertTrue(str(copy2.call_args.args[1]).startswith("\\\\?\\"))
        self.assertGreater(len(str(copy2.call_args.args[1])), 260)

    def test_detached_false_dirty_prunes_after_diff_quiet_proof_and_cleans_provisional_recovery(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "false-dirty-detached"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        head = git(detached, "rev-parse", "HEAD").stdout.strip()
        branch = "closeout/recovery/detached/test-false-dirty"
        preservation_path = repo / ".claude-state" / "closeout" / "repo-sweep" / "detached-preserve" / "closeout-recovery-detached-test-false-dirty"
        preservation_path.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-b", branch, str(preservation_path), head)
        report = {
            "candidateId": "candidate:repo-sweep-worktree-investigate:false-dirty",
            "head": head,
            "preservationBranch": branch,
            "scope": {"dirtyPaths": ["README.md"]},
            "worktree": {"path": str(detached), "head": head},
        }
        false_status = [{"status": " M", "path": "README.md", "originalPath": None}]

        with mock.patch("tools.repo_hygiene.brokered_closeout.parse_status_paths", return_value=false_status):
            action = apply_detached_dirty_preserve(repo, load_closeout_config(repo), {}, report)

        self.assertEqual(action["status"], "success", action)
        self.assertEqual(action["action"], "detached_false_dirty_prune")
        self.assertTrue(action["falseDirtyProof"]["eligible"])
        self.assertFalse(detached.exists())
        self.assertFalse(preservation_path.exists())
        self.assertNotEqual(git(repo, "rev-parse", "--verify", branch, check=False).returncode, 0)

    def test_dirty_detached_worktree_removal_refuses_missing_byte_preservation(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "dirty-detached-byte-preservation-block"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / "README.md").write_text("detached dirty readme\n", encoding="utf-8")
        (detached / "detached-only.txt").write_text("untracked bytes\n", encoding="utf-8")

        with mock.patch(
            "tools.repo_hygiene.brokered_closeout.write_dirty_worktree_recovery_evidence",
            return_value={"status": "blocked", "reason": "tracked_diff_missing", "artifactPath": ".claude-state/closeout/manual-prune/missing.json"},
        ):
            result = repo_sweep(repo, apply=True)

        action = next(item for item in result["actions"] if item.get("reason") == "tracked_diff_missing")
        self.assertEqual(action["status"], "blocked")
        self.assertTrue(detached.exists())
        self.assertEqual((detached / "detached-only.txt").read_text(encoding="utf-8"), "untracked bytes\n")

    def test_repo_sweep_detached_dirty_preservation_refuses_stale_or_missing_commit_before_cleanup(self) -> None:
        repo = self.init_repo()
        detached = self.tempdir / "dirty-detached-stale-preserve"
        git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / "README.md").write_text("detached dirty readme\n", encoding="utf-8")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["sourceDisposition"] == "retain_dirty_detached_worktree")
        git(repo, "branch", report["preservationBranch"], "HEAD")
        (repo / "stale-preservation.txt").write_text("stale preservation\n", encoding="utf-8")
        git(repo, "add", "stale-preservation.txt")
        git(repo, "commit", "-m", "stale preservation branch input")
        git(repo, "branch", "-f", report["preservationBranch"], "HEAD")

        result = repo_sweep(repo, apply=True)

        action = next(item for item in result["actions"] if item.get("reason") == "preservation_branch_exists")
        self.assertEqual(action["status"], "blocked")
        self.assertTrue(detached.exists())
        self.assertEqual((detached / "README.md").read_text(encoding="utf-8"), "detached dirty readme\n")

        missing_commit_repo = self.init_repo()
        missing_detached = self.tempdir / "dirty-detached-missing-preserve"
        git(missing_commit_repo, "worktree", "add", "--detach", str(missing_detached), "HEAD")
        (missing_detached / "README.md").write_text("detached dirty readme\n", encoding="utf-8")

        real_run_git = __import__("tools.repo_hygiene.brokered_closeout", fromlist=["run_git"]).run_git

        def fail_preservation_commit(cwd: Path, args: list[str], *pargs: object, **kwargs: object):
            if args[:2] == ["commit", "-m"] and "detached-preserve" in str(cwd):
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="simulated missing preservation commit")
            return real_run_git(cwd, args, *pargs, **kwargs)

        with mock.patch("tools.repo_hygiene.brokered_closeout.run_git", side_effect=fail_preservation_commit):
            missing_result = repo_sweep(missing_commit_repo, apply=True)

        missing_action = next(item for item in missing_result["actions"] if item.get("reason") == "preservation_commit_failed")
        self.assertEqual(missing_action["status"], "blocked")
        self.assertTrue(missing_detached.exists())

    def test_repo_sweep_explicit_protected_stale_worktree_cleanup_requires_exact_policy(self) -> None:
        repo = self.init_repo(
            config_updates={
                "repoSweep": {"lockedWorktreeStaleHours": 0},
                "cleanupPolicy": {"protectedWorktreeRoots": [".protected-worktrees/**"]},
            }
        )
        git(repo, "checkout", "-b", "codex/protected-lock")
        (repo / "protected.txt").write_text("protected lock work\n", encoding="utf-8")
        git(repo, "add", "protected.txt")
        git(repo, "commit", "-m", "protected lock branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/protected-lock", "-m", "merge protected lock")
        protected_root = repo / ".protected-worktrees"
        protected_root.mkdir(exist_ok=True)
        locked_worktree = protected_root / "locked"
        git(repo, "worktree", "add", str(locked_worktree), "codex/protected-lock")
        lock_reason = "pid=999999 protected stale test"
        git(repo, "worktree", "lock", "--reason", lock_reason, str(locked_worktree))

        plan = repo_sweep(repo)
        retained = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/protected-lock")
        self.assertEqual(retained["actionClass"], "active_locked_worktree")
        self.assertEqual(retained["recommendedAction"], "retain_with_proven_blocker")
        self.assertIn("repoSweep.protectedLockedWorktreeExactPolicy.missing_or_mismatched", retained["remediationProof"]["excludedByExactPolicy"])
        blocked_apply = repo_sweep(repo, apply=True)
        blocked_report = next(item for item in blocked_apply["retainedCandidateReports"] if item["branch"] == "codex/protected-lock")
        self.assertEqual(blocked_report["actionClass"], "active_locked_worktree")
        self.assertTrue(locked_worktree.exists())
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/protected-lock").returncode, 0)
        evidence = retained["scope"]["protectedWorktreeCleanupEvidence"]
        self.write_config(
            repo,
            {
                "repoSweep": {"lockedWorktreeStaleHours": 0},
                "cleanupPolicy": {"protectedWorktreeRoots": [".protected-worktrees/**"]},
                "repoSweep": {
                    "lockedWorktreeStaleHours": 0,
                    "protectedLockedWorktreeExactPolicy": [
                        {
                            "branch": "codex/protected-lock",
                            "path": evidence["path"],
                            "lockReason": lock_reason,
                            "action": "cleanup_worktree_and_prune",
                            "evidenceHash": evidence["evidenceHash"],
                            "recoveryCommand": "restore branch from reflog if cleanup was wrong",
                        }
                    ],
                },
            },
        )

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/protected-lock")
        self.assertEqual(report["actionClass"], "explicit_protected_worktree_cleanup")
        self.assertFalse(locked_worktree.exists())
        self.assertNotEqual(git(repo, "rev-parse", "--verify", "codex/protected-lock", check=False).returncode, 0)

    def test_repo_sweep_protected_locked_worktree_without_exact_policy_is_inspect_only(self) -> None:
        repo = self.init_repo(
            config_updates={
                "repoSweep": {"lockedWorktreeStaleHours": 0},
                "cleanupPolicy": {"protectedWorktreeRoots": [".protected-worktrees/**"]},
            }
        )
        git(repo, "checkout", "-b", "codex/protected-inspect")
        (repo / "protected.txt").write_text("protected lock work\n", encoding="utf-8")
        git(repo, "add", "protected.txt")
        git(repo, "commit", "-m", "protected lock branch")
        git(repo, "checkout", "master")
        git(repo, "merge", "--no-ff", "codex/protected-inspect", "-m", "merge protected lock")
        protected_root = repo / ".protected-worktrees"
        protected_root.mkdir(exist_ok=True)
        locked_worktree = protected_root / "locked-inspect"
        git(repo, "worktree", "add", str(locked_worktree), "codex/protected-inspect")
        git(repo, "worktree", "lock", "--reason", "pid=999999 protected inspect test", str(locked_worktree))

        result = repo_sweep(repo, apply=True)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/protected-inspect")
        self.assertEqual(report["actionClass"], "active_locked_worktree")
        self.assertEqual(report["recommendedAction"], "retain_with_proven_blocker")
        self.assertTrue(locked_worktree.exists())
        self.assertEqual(git(repo, "rev-parse", "--verify", "codex/protected-inspect").returncode, 0)
        self.assertFalse(any(item.get("branch") == "codex/protected-inspect" for item in result["actions"]))

    def test_repo_sweep_merge_failed_report_has_agent_resolution_packet(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/conflict")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/conflict")
        self.assertEqual(report["recommendedAction"], "dispatch_conflict_remediation")
        self.assertEqual(report["actionClass"], "agent_conflict_remediation")
        self.assertEqual(report["scope"]["mergeProbe"]["reason"], "merge_failed")
        self.assertEqual(report["scope"]["mergeProbe"]["conflicts"], ["conflict.txt"])
        self.assertEqual(report["scope"]["agentResolutionPacket"]["symbolicAction"], "resolve-conflicts-with-agent")

    def test_repo_sweep_merge_failed_promotes_agent_conflict_dispatch(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/conflict-promote")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")

        result = repo_sweep(repo)

        self.assertTrue(any(item["actionId"] == "resolve_conflicts_with_agent" for item in result["promotedCandidates"]))
        promoted = next(item for item in result["promotedCandidates"] if item["pinnedRefs"]["branch"]["branch"] == "codex/conflict-promote")
        self.assertEqual(promoted["actionClass"], "agent_conflict_remediation")
        self.assertEqual(promoted["actionId"], "resolve_conflicts_with_agent")

    def test_repo_sweep_agent_conflict_dispatch_writes_queue_packet(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/conflict-dispatch")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/conflict-dispatch")

        result = repo_sweep(repo, apply=True, candidate_id=report["candidateId"])

        action = next(item for item in result["actions"] if item.get("action") == "agent_conflict_remediation_dispatch")
        self.assertEqual(action["status"], "queued", action)
        queue_path = Path(action["queuePath"])
        self.assertTrue(queue_path.exists())
        packet = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["actionId"], "resolve_conflicts_with_agent")
        self.assertEqual(packet["branch"], "codex/conflict-dispatch")
        self.assertEqual(packet["conflictPaths"], ["conflict.txt"])
        self.assertIn("workBlockId", packet)
        self.assertTrue(packet["requireExactTuple"])
        self.assertEqual(packet["exactTuple"]["candidateId"], packet["candidateId"])
        self.assertEqual(packet["exactTuple"]["actionId"], packet["actionId"])
        self.assertEqual(packet["exactTuple"]["evidenceHash"], packet["evidenceHash"])
        self.assertEqual(packet["exactTuple"]["policyHash"], packet["policyHash"])
        self.assertEqual(packet["exactTuple"]["pinnedRefs"], packet["pinnedRefs"])
        self.assertIn("dirtyStateHash", packet)
        self.assertIn("resultRoot", packet)
        self.assertIn("validationRequirements", packet)
        self.assertIn("expectedOutputSchema", packet)
        self.assertEqual(packet["surfaceUnavailableStatus"], "agent_remediation_surface_unavailable")
        self.assertEqual(len(packet["shards"]), 1)
        shard = packet["shards"][0]
        self.assertEqual(shard["candidateId"], packet["candidateId"])
        self.assertEqual(shard["actionId"], packet["actionId"])
        self.assertEqual(shard["evidenceHash"], packet["evidenceHash"])
        self.assertEqual(shard["policyHash"], packet["policyHash"])
        self.assertEqual(shard["pinnedRefs"], packet["pinnedRefs"])
        self.assertEqual(shard["allowedWriteScope"], ["conflict.txt"])
        self.assertIn("conflict.txt", shard["allowedReadScope"])
        self.assertTrue(shard["resultPath"].endswith(".json"))
        self.assertEqual(shard["perAgentTimeoutMs"], 600000)
        self.assertEqual(shard["maxAgentOutputBytes"], 1048576)
        self.assertIn("agent_remediation_dispatch", self.audit_types(repo))

    def test_agent_remediation_queue_policy_fields_are_contract_required(self) -> None:
        config = load_closeout_config(ROOT)
        contract = broker_contract(ROOT)

        self.assertIn("agentRemediationQueue", contract["requiredConfigKeys"])
        self.assertIn("agentRemediationQueue", config["toolingBaseline"]["requiredConfigKeys"])
        queue = config["agentRemediationQueue"]
        for key in [
            "enabled",
            "queueRoots",
            "resultRoot",
            "maxParallelAgents",
            "perAgentTimeoutMs",
            "maxAgentOutputBytes",
            "requireExactTuple",
            "surfaceUnavailableStatus",
        ]:
            self.assertIn(key, queue)
        self.assertIn("protectedTargetNoopCloseout", config["hardClean"])
        self.assertTrue(config["hardClean"]["protectedTargetNoopCloseout"]["enabled"])

    def test_codex_agent_queue_consumer_plans_one_background_agent_per_eligible_shard(self) -> None:
        repo = self.init_repo(config_updates={"agentRemediation": {"maxConflictFilesPerAgent": 1}, "agentRemediationQueue": {"maxParallelAgents": 2}})
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp"]:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("base\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/queue-consume")
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp"]:
            (repo / path).write_text("branch\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "branch conflicts")
        git(repo, "checkout", "master")
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp"]:
            (repo / path).write_text("target\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "target conflicts")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/queue-consume")
        repo_sweep(repo, apply=True, candidate_id=report["candidateId"])

        result = agent_remediation_queue_consumer_plan(repo, surface="codex-desktop")

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["maxParallelAgents"], 2)
        self.assertEqual(len(result["spawnPlan"]), 2)
        for item in result["spawnPlan"]:
            self.assertEqual(item["surface"], "codex-desktop")
            self.assertEqual(item["actionId"], "resolve_conflicts_with_agent")
            self.assertEqual(item["mutationBoundary"], "repo-owned symbolic actors only")
            self.assertTrue(item["allowedWriteScope"])
            self.assertTrue(item["resultPath"].endswith(".json"))

    def test_agent_queue_surface_unavailable_writes_result_packets(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/unavailable")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/unavailable")
        repo_sweep(repo, apply=True, candidate_id=report["candidateId"])

        result = agent_remediation_queue_consumer_plan(repo, surface="no-agent-surface", mark_unavailable=True)

        self.assertEqual(result["status"], "agent_remediation_surface_unavailable", result)
        self.assertEqual(len(result["unavailableResults"]), 1)
        result_path = Path(result["unavailableResults"][0]["resultPath"])
        self.assertTrue(result_path.exists())
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "agent_remediation_surface_unavailable")
        self.assertEqual(payload["allowedWriteScope"], ["conflict.txt"])
        self.assertIn("agent_remediation_surface_unavailable", self.audit_types(repo))

    def test_agent_queue_stale_packets_rejected_on_policy_hash_or_dirty_state_change(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/stale-queue")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/stale-queue")
        repo_sweep(repo, apply=True, candidate_id=report["candidateId"])
        (repo / "new-dirty.txt").write_text("dirty after packet\n", encoding="utf-8")

        result = agent_remediation_queue_consumer_plan(repo)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["spawnPlan"], [])
        stale_reasons = {reason["kind"] for packet in result["stalePackets"] for reason in packet["staleReasons"]}
        self.assertIn("dirty_state_changed", stale_reasons)
        self.assertIn("agent_remediation_queue_stale", self.audit_types(repo))
        self.assertNotIn("agent_remediation_stale_retired_packet_reap", self.audit_types(repo))

    def test_agent_queue_dirty_hash_detects_same_status_byte_change(self) -> None:
        repo = self.init_repo()
        dirty = repo / "dirty.txt"
        dirty.write_text("one\n", encoding="utf-8")
        before = agent_remediation_dirty_state_hash(repo)
        dirty.write_text("two\n", encoding="utf-8")
        after = agent_remediation_dirty_state_hash(repo)

        self.assertNotEqual(before, after)

    def test_agent_queue_stale_packet_blocks_even_when_valid_shard_can_spawn(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(repo, candidate_id="candidate:valid")
        stale_path = self.write_agent_queue_packet(repo, candidate_id="candidate:stale")
        packet = json.loads(stale_path.read_text(encoding="utf-8"))
        packet["policyHash"] = "stale-policy"
        packet["exactTuple"]["policyHash"] = "stale-policy"
        stale_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        result = agent_remediation_queue_consumer_plan(repo)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(len(result["spawnPlan"]), 1)
        self.assertEqual(result["spawnPlan"][0]["candidateId"], "candidate:valid")
        stale_reasons = {reason["kind"] for packet in result["stalePackets"] for reason in packet["staleReasons"]}
        self.assertIn("policy_hash_mismatch", stale_reasons)

    def test_agent_queue_remote_fetch_failure_blocks_stale_check(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        pinned_refs = {"target": {"remote": "missing-remote", "branch": "master", "ref": "refs/remotes/missing-remote/master", "head": "0" * 40}}
        exact_tuple = {
            "candidateId": "candidate:missing-remote",
            "actionId": "resolve_conflicts_with_agent",
            "evidenceHash": "manual-evidence",
            "policyHash": config["policyHash"],
            "pinnedRefs": pinned_refs,
        }
        self.write_agent_queue_packet(
            repo,
            candidate_id="candidate:missing-remote",
            updates={
                "pinnedRefs": pinned_refs,
                "exactTuple": exact_tuple,
                "shards": [
                    {
                        "shardId": "remote-01",
                        "candidateId": "candidate:missing-remote",
                        "workBlockId": None,
                        "actionId": "resolve_conflicts_with_agent",
                        "evidenceHash": "manual-evidence",
                        "policyHash": config["policyHash"],
                        "pinnedRefs": pinned_refs,
                        "allowedReadScope": ["conflict.txt"],
                        "allowedWriteScope": ["conflict.txt"],
                        "resultPath": ".claude-state/closeout/agent-remediation/results/missing-remote/remote-01.json",
                        "expectedOutputSchema": {},
                        "validationRequirements": [],
                    }
                ],
            },
        )

        result = agent_remediation_queue_consumer_plan(repo)

        self.assertEqual(result["status"], "blocked", result)
        stale_reasons = {reason["kind"] for packet in result["stalePackets"] for reason in packet["staleReasons"]}
        self.assertIn("remote_fetch_failed", stale_reasons)

    def test_agent_queue_result_path_outside_result_root_is_stale(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(
            repo,
            shards=[
                {
                    "shardId": "bad-01",
                    "candidateId": "candidate:bad-path",
                    "workBlockId": None,
                    "actionId": "resolve_conflicts_with_agent",
                    "evidenceHash": "manual-evidence",
                    "policyHash": load_closeout_config(repo)["policyHash"],
                    "pinnedRefs": {},
                    "allowedReadScope": ["conflict.txt"],
                    "allowedWriteScope": ["conflict.txt"],
                    "resultPath": "../outside-result.json",
                    "expectedOutputSchema": {},
                    "validationRequirements": [],
                }
            ],
            candidate_id="candidate:bad-path",
        )

        result = agent_remediation_queue_consumer_plan(repo)

        self.assertEqual(result["status"], "blocked", result)
        stale_reasons = {reason["kind"] for packet in result["stalePackets"] for reason in packet["staleReasons"]}
        self.assertIn("result_path_outside_result_root", stale_reasons)

    def test_agent_queue_skips_completed_result_paths_before_planning_more_shards(self) -> None:
        repo = self.init_repo(config_updates={"agentRemediationQueue": {"maxParallelAgents": 2}})
        config = load_closeout_config(repo)
        shards = []
        for index in range(1, 4):
            shards.append(
                {
                    "shardId": f"manual-{index:02d}",
                    "candidateId": "candidate:three-shards",
                    "workBlockId": None,
                    "actionId": "resolve_conflicts_with_agent",
                    "evidenceHash": "manual-evidence",
                    "policyHash": config["policyHash"],
                    "pinnedRefs": {},
                    "allowedReadScope": [f"conflict{index}.txt"],
                    "allowedWriteScope": [f"conflict{index}.txt"],
                    "resultPath": f".claude-state/closeout/agent-remediation/results/three/manual-{index:02d}.json",
                    "expectedOutputSchema": {},
                    "validationRequirements": [],
                }
            )
        self.write_agent_queue_packet(repo, candidate_id="candidate:three-shards", shards=shards)
        completed = repo / shards[0]["resultPath"]
        completed.parent.mkdir(parents=True, exist_ok=True)
        completed.write_text("{}", encoding="utf-8")

        result = agent_remediation_queue_consumer_plan(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual([item["shardId"] for item in result["spawnPlan"]], ["manual-02", "manual-03"])

    def test_agent_result_collection_blocks_agent_reported_blockers_and_failed_validation(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(repo, candidate_id="candidate:blocking-result")
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = repo / spawn["resultPath"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "blocked",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "cannot resolve",
                    "changedPaths": [],
                    "blockers": [{"kind": "semantic_conflict"}],
                    "validation": [{"name": "unit", "returncode": 1}],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = collect_agent_remediation_results(repo)

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("agent_result_not_resolved", blocker_kinds)
        self.assertIn("agent_result_blocked", blocker_kinds)
        self.assertIn("agent_result_validation_failed", blocker_kinds)

    def test_agent_result_collection_rejects_missing_required_result_fields(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(repo, candidate_id="candidate:missing-fields")
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = repo / spawn["resultPath"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = collect_agent_remediation_results(repo)

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("agent_result_schema_invalid", blocker_kinds)

    def test_agent_result_collection_rejects_wrong_shard_identity(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(repo, candidate_id="candidate:wrong-shard")
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = repo / spawn["resultPath"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": "different-shard",
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "wrong shard id",
                    "changedPaths": ["conflict.txt"],
                    "blockers": [],
                    "validation": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = collect_agent_remediation_results(repo)

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("agent_result_identity_mismatch", blocker_kinds)

    def test_repo_closed_postcondition_blocks_pending_agent_remediation_queue(self) -> None:
        repo = self.init_repo()
        self.write_agent_queue_packet(repo, candidate_id="candidate:pending-result")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("agent_remediation_queue_not_closed", blocker_kinds)

    def test_repo_closed_postcondition_blocks_unreadable_or_unretired_queue_artifacts(self) -> None:
        repo = self.init_repo()
        done_path = self.write_agent_queue_packet(repo, candidate_id="candidate:done-without-proof")
        packet = json.loads(done_path.read_text(encoding="utf-8"))
        packet["status"] = "done"
        done_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        corrupt_path = done_path.parent / "corrupt.json"
        corrupt_path.write_text("{not-json", encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        agent_blocker = next(item for item in result["blockers"] if item["kind"] == "agent_remediation_queue_not_closed")
        stale_reasons = {
            reason["kind"]
            for packet in agent_blocker["agentRemediationState"]["packets"]
            for reason in packet["staleReasons"]
        }
        self.assertIn("queue_packet_not_pending_without_retirement", stale_reasons)

    def test_repo_closed_postcondition_blocks_invalid_queue_retirement_proof(self) -> None:
        repo = self.init_repo()
        path = self.write_agent_queue_packet(repo, candidate_id="candidate:invalid-retirement")
        packet = json.loads(path.read_text(encoding="utf-8"))
        packet["status"] = "retired"
        packet["retirementProof"] = {
            "candidateId": packet["candidateId"],
            "actionId": packet["actionId"],
            "evidenceHash": packet["evidenceHash"],
            "policyHash": load_closeout_config(repo)["policyHash"],
            "pinnedRefs": packet["pinnedRefs"],
            "exactTuple": packet["exactTuple"],
            "resultCollectionStatus": "success",
            "resultCollectionHash": "forged-collection-hash",
            "resultCollectionPath": ".claude-state/closeout/agent-remediation/results/collections/forged.json",
            "retiredPacketStatus": "retired",
        }
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        agent_blocker = next(item for item in result["blockers"] if item["kind"] == "agent_remediation_queue_not_closed")
        stale_reasons = {
            reason["kind"]
            for packet in agent_blocker["agentRemediationState"]["packets"]
            for reason in packet["staleReasons"]
        }
        self.assertIn("queue_packet_not_pending_without_retirement", stale_reasons)

    def test_repo_closed_postcondition_accepts_valid_queue_retirement_proof(self) -> None:
        repo = self.init_repo()
        path = self.write_agent_queue_packet(repo, candidate_id="candidate:valid-retirement")
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = repo / spawn["resultPath"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "resolved",
                    "changedPaths": ["conflict.txt"],
                    "blockers": [],
                    "validation": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        collection = collect_agent_remediation_results(repo)
        self.assertEqual(collection["status"], "success", collection)
        packet = json.loads(path.read_text(encoding="utf-8"))
        packet["status"] = "retired"
        packet["retirementProof"] = {
            "candidateId": packet["candidateId"],
            "actionId": packet["actionId"],
            "evidenceHash": packet["evidenceHash"],
            "policyHash": load_closeout_config(repo)["policyHash"],
            "pinnedRefs": packet["pinnedRefs"],
            "exactTuple": packet["exactTuple"],
            "resultCollectionStatus": "success",
            "resultCollectionHash": collection["resultCollectionHash"],
            "resultCollectionPath": collection["resultCollectionPath"],
            "retiredPacketStatus": "retired",
        }
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["agentRemediationState"]["packetCount"], 0)

    def test_agent_queue_retires_stale_plan_absent_packet_with_valid_proof(self) -> None:
        repo = self.init_repo()
        path = self.write_agent_queue_packet(repo, candidate_id="candidate:repo-sweep-action:orphaned-stale")
        packet = json.loads(path.read_text(encoding="utf-8"))
        packet["policyHash"] = "stale-policy-before-reviewed-config-change"
        packet["exactTuple"]["policyHash"] = packet["policyHash"]
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        result = retire_stale_plan_absent_agent_queue_packets(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual([item["candidateId"] for item in result["retiredPackets"]], ["candidate:repo-sweep-action:orphaned-stale"])
        retired_packet = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(retired_packet["status"], "retired")
        self.assertEqual(retired_packet["retirementReason"], "stale_plan_absent_queue_packet")
        proof = retired_packet["retirementProof"]
        self.assertEqual(proof["retiredPacketStatus"], "retired")
        self.assertEqual(proof["resultCollectionStatus"], "success")
        collection_path = repo / proof["resultCollectionPath"]
        self.assertTrue(collection_path.exists())
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        self.assertEqual(collection["resultCollectionHash"], proof["resultCollectionHash"])
        self.assertEqual(collection["collectedResults"][0]["status"], "retired_plan_absent")
        self.assertEqual(collection["collectedResults"][0]["exactTuple"], retired_packet["exactTuple"])
        self.assertIn("agent_remediation_queue_packet_retired", self.audit_types(repo))

        closeout = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(closeout["status"], "success", closeout)
        self.assertEqual(closeout["agentRemediationState"]["packetCount"], 0)

    def test_agent_queue_retirement_remote_fetch_failure_blocks_without_retiring_packet(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        pinned_refs = {"target": {"remote": "missing-remote", "branch": "master", "ref": "refs/remotes/missing-remote/master", "head": "0" * 40}}
        exact_tuple = {
            "candidateId": "candidate:repo-sweep-action:missing-remote-stale",
            "actionId": "resolve_conflicts_with_agent",
            "evidenceHash": "manual-evidence",
            "policyHash": "stale-policy-before-reviewed-config-change",
            "pinnedRefs": pinned_refs,
        }
        path = self.write_agent_queue_packet(
            repo,
            candidate_id="candidate:repo-sweep-action:missing-remote-stale",
            updates={"policyHash": exact_tuple["policyHash"], "pinnedRefs": pinned_refs, "exactTuple": exact_tuple},
        )

        result = retire_stale_plan_absent_agent_queue_packets(repo)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["reason"], "remote_fetch_failed")
        self.assertEqual(result["retiredPackets"], [])
        self.assertEqual(result["blockers"][0]["kind"], "remote_fetch_failed")
        self.assertIn("remote_fetch_failed", {failure["kind"] for failure in result["blockers"][0]["remoteFailures"]})
        packet = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(packet["status"], "queued")
        self.assertNotIn("retirementProof", packet)
        self.assertNotIn("agent_remediation_queue_packet_retired", self.audit_types(repo))

    def test_repo_closed_postcondition_accepts_retired_queue_after_policy_drift(self) -> None:
        repo = self.init_repo()
        config = load_closeout_config(repo)
        retired_target_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        split_branch = "closeout/split/wb-retired-policy-drift"
        git(repo, "checkout", "-b", split_branch)
        split_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "master")
        pinned_refs = {
            "target": {"branch": "master", "head": retired_target_head, "mode": "local", "ref": "refs/heads/master"},
            "branch": {"branch": split_branch, "head": split_head},
            "worktree": {"branch": split_branch, "branchRef": f"refs/heads/{split_branch}", "head": split_head, "path": str(repo / ".claude-state" / "closeout" / "dirty-splits" / "worktrees" / "retired-policy-drift")},
        }
        shards = [
            {
                "shardId": "manual-01",
                "candidateId": "candidate:retired-policy-drift",
                "workBlockId": None,
                "actionId": "resolve_conflicts_with_agent",
                "evidenceHash": "manual-evidence",
                "policyHash": config["policyHash"],
                "pinnedRefs": pinned_refs,
                "allowedReadScope": ["conflict.txt"],
                "allowedWriteScope": ["conflict.txt"],
                "resultPath": ".claude-state/closeout/agent-remediation/results/manual/retired-policy-drift.json",
                "expectedOutputSchema": {"requiredFields": ["status", "changedPaths"]},
                "validationRequirements": [],
            }
        ]
        path = self.write_agent_queue_packet(
            repo,
            candidate_id="candidate:retired-policy-drift",
            shards=shards,
            updates={"pinnedRefs": pinned_refs, "exactTuple": {"pinnedRefs": pinned_refs}},
        )
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = repo / spawn["resultPath"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "resolved before policy drift",
                    "changedPaths": ["conflict.txt"],
                    "blockers": [],
                    "validation": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        collection = collect_agent_remediation_results(repo)
        self.assertEqual(collection["status"], "success", collection)
        packet = json.loads(path.read_text(encoding="utf-8"))
        packet["status"] = "retired"
        packet["retirementProof"] = {
            "candidateId": packet["candidateId"],
            "actionId": packet["actionId"],
            "evidenceHash": packet["evidenceHash"],
            "policyHash": packet["policyHash"],
            "pinnedRefs": packet["pinnedRefs"],
            "exactTuple": packet["exactTuple"],
            "resultCollectionStatus": "success",
            "resultCollectionHash": collection["resultCollectionHash"],
            "resultCollectionPath": collection["resultCollectionPath"],
            "retiredPacketStatus": "retired",
        }
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        git(repo, "branch", "-D", split_branch)
        self.write_config(repo, {"policyHash": "new-policy-after-retirement"})
        (repo / "README.md").write_text("hello\nadvance target after retirement\n", encoding="utf-8")
        git(repo, "add", "closeout.config.json", "README.md")
        git(repo, "commit", "-m", "change closeout policy")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["agentRemediationState"]["packetCount"], 0)
        self.assertIn("agent_remediation_stale_retired_packet_reap", self.audit_types(repo))

    def test_repo_closed_postcondition_rejects_wrong_result_tuple(self) -> None:
        repo = self.init_repo()
        path = self.write_agent_queue_packet(repo, candidate_id="candidate:wrong-active-tuple")
        packet = json.loads(path.read_text(encoding="utf-8"))
        shard = packet["shards"][0]
        result_path = repo / shard["resultPath"]
        wrong_tuple = dict(packet["exactTuple"])
        wrong_tuple["evidenceHash"] = "stale-evidence"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": packet["candidateId"],
                    "shardId": shard["shardId"],
                    "workBlockId": packet["workBlockId"],
                    "actionId": packet["actionId"],
                    "evidenceHash": wrong_tuple["evidenceHash"],
                    "policyHash": packet["policyHash"],
                    "pinnedRefs": packet["pinnedRefs"],
                    "exactTuple": wrong_tuple,
                    "summary": "resolved from a stale tuple",
                    "changedPaths": [],
                    "blockers": [],
                    "validation": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        agent_blocker = next(item for item in result["blockers"] if item["kind"] == "agent_remediation_queue_not_closed")
        blocker_kinds = {item["kind"] for item in agent_blocker["agentRemediationState"]["blockers"]}
        self.assertIn("agent_remediation_result_tuple_mismatch", blocker_kinds)

    def test_repo_closed_postcondition_rejects_stale_collection_retirement_tuple(self) -> None:
        repo = self.init_repo()
        path = self.write_agent_queue_packet(repo, candidate_id="candidate:stale-collection")
        config = load_closeout_config(repo)
        packet = json.loads(path.read_text(encoding="utf-8"))
        wrong_tuple = dict(packet["exactTuple"])
        wrong_tuple["evidenceHash"] = "stale-evidence"
        payload = {
            "status": "success",
            "packetCount": 1,
            "collectedResults": [
                {
                    "candidateId": packet["candidateId"],
                    "shardId": packet["shards"][0]["shardId"],
                    "workBlockId": packet["workBlockId"],
                    "actionId": packet["actionId"],
                    "evidenceHash": wrong_tuple["evidenceHash"],
                    "policyHash": packet["policyHash"],
                    "pinnedRefs": packet["pinnedRefs"],
                    "exactTuple": wrong_tuple,
                    "resultPath": packet["shards"][0]["resultPath"],
                    "status": "resolved",
                    "changedPaths": [],
                    "blockers": [],
                }
            ],
            "blockers": [],
            "nextSymbolicAction": {
                "action": "repo_owned_coordinator_revalidate_and_finalize",
                "allowedActors": ["repo-sweep", "finalize-closeout", "remediate-retained"],
                "mutationBoundary": "repo-owned symbolic actors only",
            },
        }
        collection_hash = stable_hash(payload)
        collection_path = repo / ".claude-state" / "closeout" / "agent-remediation" / "results" / "collections" / f"{collection_hash}.json"
        collection_path.parent.mkdir(parents=True, exist_ok=True)
        collection_path.write_text(
            json.dumps(
                {
                    **payload,
                    "resultCollectionHash": collection_hash,
                    "resultCollectionPath": ".claude-state/closeout/agent-remediation/results/collections/%s.json" % collection_hash,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        packet["status"] = "retired"
        packet["retirementProof"] = {
            "candidateId": packet["candidateId"],
            "actionId": packet["actionId"],
            "evidenceHash": packet["evidenceHash"],
            "policyHash": config["policyHash"],
            "pinnedRefs": packet["pinnedRefs"],
            "exactTuple": packet["exactTuple"],
            "resultCollectionStatus": "success",
            "resultCollectionHash": collection_hash,
            "resultCollectionPath": ".claude-state/closeout/agent-remediation/results/collections/%s.json" % collection_hash,
            "retiredPacketStatus": "retired",
        }
        path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

        result = verify_repo_closed_postcondition(repo, work_block_id=None, finalize_result={"status": "success"})

        self.assertEqual(result["status"], "blocked", result)
        agent_blocker = next(item for item in result["blockers"] if item["kind"] == "agent_remediation_queue_not_closed")
        stale_reasons = {
            reason["kind"]
            for packet_row in agent_blocker["agentRemediationState"]["packets"]
            for reason in packet_row["staleReasons"]
        }
        self.assertIn("queue_packet_not_pending_without_retirement", stale_reasons)

    def test_agent_result_collection_rejects_out_of_scope_changed_paths(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/result-scope")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/result-scope")
        repo_sweep(repo, apply=True, candidate_id=report["candidateId"])
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = Path(spawn["resultPath"])
        if not result_path.is_absolute():
            result_path = repo / result_path
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "attempted out-of-scope change",
                    "changedPaths": ["foreign.txt"],
                    "blockers": [],
                    "validation": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = collect_agent_remediation_results(repo)

        self.assertEqual(result["status"], "blocked", result)
        blocker_kinds = {item["kind"] for item in result["blockers"]}
        self.assertIn("agent_scope_violation", blocker_kinds)
        self.assertIn("agent_remediation_result_collection", self.audit_types(repo))

    def test_agent_result_collection_returns_symbolic_next_action_without_mutation(self) -> None:
        repo = self.init_repo()
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/result-symbolic")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")
        plan = repo_sweep(repo)
        report = next(item for item in plan["retainedCandidateReports"] if item["branch"] == "codex/result-symbolic")
        repo_sweep(repo, apply=True, candidate_id=report["candidateId"])
        spawn = agent_remediation_queue_consumer_plan(repo)["spawnPlan"][0]
        result_path = Path(spawn["resultPath"])
        if not result_path.is_absolute():
            result_path = repo / result_path
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "resolved",
                    "candidateId": spawn["candidateId"],
                    "shardId": spawn["shardId"],
                    "workBlockId": spawn["workBlockId"],
                    "actionId": spawn["actionId"],
                    "evidenceHash": spawn["exactTuple"]["evidenceHash"],
                    "policyHash": spawn["exactTuple"]["policyHash"],
                    "pinnedRefs": spawn["exactTuple"]["pinnedRefs"],
                    "exactTuple": spawn["exactTuple"],
                    "summary": "conflict path resolved in assigned scope",
                    "changedPaths": ["conflict.txt"],
                    "blockers": [],
                    "validation": [{"name": "unit", "returncode": 0}],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        result = collect_agent_remediation_results(repo)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["nextSymbolicAction"]["mutationBoundary"], "repo-owned symbolic actors only")
        self.assertEqual((repo / "conflict.txt").read_text(encoding="utf-8"), "target\n")
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "master")

    def test_agent_conflict_dispatch_shards_large_conflict_sets(self) -> None:
        repo = self.init_repo(config_updates={"agentRemediation": {"maxConflictFilesPerAgent": 2}})
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp", "tests/a.cpp", "tests/b.cpp"]:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("base\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/many-conflicts")
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp", "tests/a.cpp", "tests/b.cpp"]:
            (repo / path).write_text("branch\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "branch conflicts")
        git(repo, "checkout", "master")
        for path in ["src/a.cpp", "src/b.cpp", "src/c.cpp", "tests/a.cpp", "tests/b.cpp"]:
            (repo / path).write_text("target\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "target conflicts")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/many-conflicts")
        shards = report["scope"]["agentResolutionPacket"]["shards"]
        self.assertGreaterEqual(len(shards), 3)
        self.assertTrue(all(len(shard["conflictPaths"]) <= 2 for shard in shards))
        self.assertEqual(report["recommendedAction"], "dispatch_conflict_remediation")

    def test_repo_sweep_retained_terminal_outcomes_prove_remediation_attempted_or_excluded(self) -> None:
        repo = self.init_repo(config_updates={"agentRemediation": {"enabled": False}})
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "conflict base")
        git(repo, "checkout", "-b", "codex/conflict-proof")
        (repo / "conflict.txt").write_text("branch\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "branch conflict")
        git(repo, "checkout", "master")
        (repo / "conflict.txt").write_text("target\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        git(repo, "commit", "-m", "target conflict")

        result = repo_sweep(repo)

        report = next(item for item in result["retainedCandidateReports"] if item["branch"] == "codex/conflict-proof")
        proof = report["remediationProof"]
        self.assertFalse(proof["symbolicRemediationAttempted"])
        self.assertTrue(proof["policyEligible"])
        self.assertEqual(proof["excludedByExactPolicy"], ["merge_failed"])

    def test_dirty_split_auto_remediates_owned_dirty_and_retains_foreign(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/split-owned")
        start_work_block(repo, work_block_id="wb-split-owned", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        start_work_block(repo, work_block_id="wb-split-foreign-owner", actor="local-test", path_claims=["foreign.txt"])
        (repo / "owned.txt").write_text("owned dirty preserved\n", encoding="utf-8")
        (repo / "foreign.txt").write_text("foreign dirty retained\n", encoding="utf-8")

        result = preserve_owned_dirty_split(repo, work_block_id="wb-split-owned")

        self.assertEqual(result["status"], "success", result)
        preservation = result["results"][0]
        self.assertEqual((repo / "owned.txt").read_text(encoding="utf-8"), "owned committed\n")
        self.assertEqual((repo / "foreign.txt").read_text(encoding="utf-8"), "foreign dirty retained\n")
        self.assertEqual(git(repo, "show", f"{preservation['preservationBranch']}:owned.txt").stdout, "owned dirty preserved\n")
        self.assertIn("dirty_split_success", self.audit_types(repo))
        self.assertIn("auto_quorum", self.audit_types(repo))
        detection_after = detect_work_block(repo, work_block_id="wb-split-owned")
        self.assertFalse(detection_after["ownedDirty"])
        self.assertEqual([item["path"] for item in detection_after["foreignDirty"]], ["foreign.txt"])

    def test_dirty_split_stale_tuple_is_rejected_before_mutation(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/split-stale")
        start_work_block(repo, work_block_id="wb-split-stale", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        (repo / "owned.txt").write_text("first dirty\n", encoding="utf-8")
        detection = detect_work_block(repo, work_block_id="wb-split-stale")
        candidate = plan_dirty_split_candidates(repo, load_closeout_config(repo), detection)[0]
        (repo / "owned.txt").write_text("second dirty drift\n", encoding="utf-8")

        result = apply_dirty_split_candidate(repo, candidate)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "stale_tuple")
        self.assertIn("dirty_split_stale_tuple", self.audit_types(repo))
        self.assertEqual((repo / "owned.txt").read_text(encoding="utf-8"), "second dirty drift\n")

    def test_dirty_split_reuses_feature_head_branch_with_sparse_worktree(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/split-retry")
        start_work_block(repo, work_block_id="wb-split-retry", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        (repo / "owned.txt").write_text("owned dirty retry\n", encoding="utf-8")
        detection = detect_work_block(repo, work_block_id="wb-split-retry")
        candidate = plan_dirty_split_candidates(repo, load_closeout_config(repo), detection)[0]
        git(repo, "branch", candidate["preservationBranch"], detection["featureHead"])

        result = apply_dirty_split_candidate(repo, candidate)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(git(repo, "show", f"{candidate['preservationBranch']}:owned.txt").stdout, "owned dirty retry\n")
        self.assertEqual((repo / "owned.txt").read_text(encoding="utf-8"), "owned committed\n")
        sparse = git(Path(result["preservationWorktree"]), "config", "--get", "core.sparseCheckout").stdout.strip()
        self.assertEqual(sparse, "true")

    def test_dirty_split_mutates_only_one_candidate_per_run_and_audits(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/split-once")
        start_work_block(repo, work_block_id="wb-split-once", actor="local-test", path_claims=["one.txt", "two.txt"])
        (repo / "one.txt").write_text("one committed\n", encoding="utf-8")
        (repo / "two.txt").write_text("two committed\n", encoding="utf-8")
        git(repo, "add", "one.txt", "two.txt")
        git(repo, "commit", "-m", "two owned files")
        (repo / "one.txt").write_text("one dirty\n", encoding="utf-8")
        (repo / "two.txt").write_text("two dirty\n", encoding="utf-8")

        result = preserve_owned_dirty_split(repo, work_block_id="wb-split-once")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(sorted(result["candidates"][0]["paths"]), ["one.txt", "two.txt"])
        split_branches = git(repo, "for-each-ref", "refs/heads/closeout/split", "--format=%(refname:short)").stdout.splitlines()
        self.assertEqual(len(split_branches), 1)
        self.assertIn("dirty_split_success", self.audit_types(repo))

    def test_repair_runs_dirty_split_before_owned_dirty_blocks(self) -> None:
        repo = self.init_repo(config_updates={"stashPolicy": {"allowOwnedDirtyCheckpoint": False}})
        git(repo, "checkout", "-b", "codex/split-repair")
        start_work_block(repo, work_block_id="wb-split-repair", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        (repo / "owned.txt").write_text("owned dirty repair\n", encoding="utf-8")

        from .brokered_closeout import repair_eligibility

        result = repair_eligibility(repo, work_block_id="wb-split-repair")

        self.assertEqual(result["status"], "repaired", result)
        self.assertEqual(result["blockers"], [])
        self.assertEqual((repo / "owned.txt").read_text(encoding="utf-8"), "owned committed\n")
        self.assertIn("dirty_split_success", self.audit_types(repo))

    def test_repair_checkpoints_owned_dirty_while_retaining_foreign_dirty(self) -> None:
        repo = self.init_repo()
        git(repo, "checkout", "-b", "codex/checkpoint-owned")
        start_work_block(repo, work_block_id="wb-checkpoint-owned", actor="local-test", path_claims=["owned.txt"])
        (repo / "owned.txt").write_text("owned committed\n", encoding="utf-8")
        git(repo, "add", "owned.txt")
        git(repo, "commit", "-m", "owned committed")
        start_work_block(repo, work_block_id="wb-checkpoint-foreign-owner", actor="local-test", path_claims=["foreign.txt"])
        (repo / "owned.txt").write_text("owned checkpointed\n", encoding="utf-8")
        (repo / "foreign.txt").write_text("foreign retained\n", encoding="utf-8")

        from .brokered_closeout import repair_eligibility

        result = repair_eligibility(repo, work_block_id="wb-checkpoint-owned")

        self.assertEqual(result["status"], "repaired", result)
        self.assertEqual(git(repo, "show", "HEAD:owned.txt").stdout, "owned checkpointed\n")
        self.assertEqual((repo / "foreign.txt").read_text(encoding="utf-8"), "foreign retained\n")
        self.assertIn("checkpoint_owned_dirty", self.audit_types(repo))
        self.assertIn("retained_foreign_dirty", self.audit_types(repo))

    def test_missing_evidence_is_generated_and_committed_before_publish(self) -> None:
        repo = self.init_repo(
            remote=True,
            config_updates={
                "evidenceRepair": {
                    "enabled": True,
                    "evidenceRoot": ".closeout-evidence",
                    "requiredArtifacts": ["metrics.json", "handoff.json", "session.json", "closeout.json"],
                    "requiredFor": ["publish_missing_upstream"],
                    "commitMessage": "test evidence repair",
                }
            },
        )
        self.make_feature(repo, "wb-evidence-repair")

        result = repair_eligibility(repo, work_block_id="wb-evidence-repair")

        self.assertEqual(result["status"], "repaired", result)
        actions = [item["action"] for item in result["actions"]]
        self.assertLess(actions.index("evidence_repair"), actions.index("publish_missing_upstream"))
        for artifact in ["metrics.json", "handoff.json", "session.json", "closeout.json"]:
            path = f".closeout-evidence/wb-evidence-repair/{artifact}"
            self.assertEqual(git(repo, "cat-file", "-e", f"HEAD:{path}", check=False).returncode, 0)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "codex/test-work@{upstream}").stdout.strip(), "origin/codex/test-work")
        self.assertIn("evidence_repair", self.audit_types(repo))

    def test_existing_evidence_is_refreshed_after_a_later_source_commit(self) -> None:
        repo = self.init_repo(
            config_updates={
                "evidenceRepair": {
                    "enabled": True,
                    "evidenceRoot": ".closeout-evidence",
                    "requiredArtifacts": ["metrics.json", "handoff.json", "session.json", "closeout.json"],
                }
            }
        )
        self.make_feature(repo, "wb-evidence-refresh")
        initial_detection = detect_work_block(repo, work_block_id="wb-evidence-refresh")
        initial = repair_missing_evidence(repo, load_closeout_config(repo), initial_detection, reason="test")
        self.assertEqual("success", initial["status"], initial)

        (repo / "later-source.txt").write_text("later source\n", encoding="utf-8")
        git(repo, "add", "later-source.txt")
        git(repo, "commit", "-m", "later source repair")
        source_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        refreshed_detection = detect_work_block(repo, work_block_id="wb-evidence-refresh")

        refreshed = repair_missing_evidence(
            repo,
            load_closeout_config(repo),
            refreshed_detection,
            reason="candidate_acceptance_preflight",
        )

        self.assertEqual("success", refreshed["status"], refreshed)
        self.assertEqual(source_head, git(repo, "rev-parse", "HEAD^").stdout.strip())
        for artifact in ("metrics.json", "handoff.json", "session.json", "closeout.json"):
            path = f".closeout-evidence/wb-evidence-refresh/{artifact}"
            payload = json.loads(git(repo, "show", f"HEAD:{path}").stdout)
            self.assertEqual(source_head, payload["featureHead"])
            self.assertEqual("wb-evidence-refresh", payload["workBlockId"])

    def test_evidence_repair_refuses_claimed_evidence_path(self) -> None:
        repo = self.init_repo(
            remote=True,
            config_updates={
                "evidenceRepair": {
                    "enabled": True,
                    "requiredFor": ["publish_missing_upstream"],
                }
            },
        )
        self.make_feature(repo, "wb-evidence-blocked")
        start_work_block(
            repo,
            work_block_id="wb-other-evidence-owner",
            actor="local-test",
            path_claims=[".closeout-evidence/wb-evidence-blocked/metrics.json"],
        )

        result = repair_eligibility(repo, work_block_id="wb-evidence-blocked")

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(str(item).startswith("evidenceRepairFailed") for item in result["blockers"]))
        self.assertIn("evidence_repair_blocked", self.audit_types(repo))


class CoordinationGateIdentityTests(unittest.TestCase):
    """Reader-side gate hardening (opus SEQ 483 spec + 485 amendment, fable SEQ 950/951).

    Phase 1: actor/kind are captured from the heading grammar, never substring-matched
    against free text. Phase 2: the decisive APPROVING entry must carry a Seat GUID in
    contentReviewGate.authorizedReviewSessions when that allowlist is configured.
    """

    START = "c" * 40
    FEATURE = "f" * 40

    # A REAL heading from the live gate file (2026-06-21): under the old substring
    # matcher this satisfied actor=CODEX/kind=REVIEW purely from its own prose.
    REAL_FALSE_POSITIVE_HEADING = (
        "### [2026-06-21T14:27:05-05:00] CLAUDE - HANDOFF "
        "(DNG-folder playback first cut; ROLE INVERSION: Codex please REVIEW + adopt + merge)"
    )

    def range_token(self) -> str:
        return "%s..%s" % (self.START, self.FEATURE)

    def entry(self, heading: str, body_lines) -> dict:
        body = heading + "\n" + "\n".join(body_lines) + "\n"
        return {"offset": 0, "heading": heading, "body": body}

    def gate_config(self, **overrides) -> dict:
        gate = {
            "requireClaudeApprovalForFinalize": True,
            "coordinationFile": "gate.md",
            "handoffActor": "CODEX",
            "handoffKind": "HANDOFF",
            "reviewActor": "CLAUDE",
            "reviewKind": "REVIEW",
            "approveTokens": ["APPROVE"],
            "blockingTokens": ["CHANGES_REQUESTED", "BLOCKER"],
            "requireHandoff": True,
        }
        gate.update(overrides)
        return {"contentReviewGate": gate}

    def run_validator(self, ledger_text: str, **gate_overrides):
        config = self.gate_config(**gate_overrides)
        detection = {"featureHead": self.FEATURE, "targetHead": self.START, "targetBranch": "master"}
        manifest = {"startHead": self.START}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gate.md").write_text(ledger_text, encoding="utf-8")
            return validate_content_review_approval_for_finalize(root, config, detection, manifest)

    def ledger(self, *entries) -> str:
        return "\n".join(entries) + "\n"

    def handoff_entry(self, actor: str = "CODEX") -> str:
        return (
            "### [2026-01-01T00:00:00Z] %s - HANDOFF (block ready)\n" % actor
            + "Range: `%s`\n" % self.range_token()
        )

    def review_entry(self, verdict: str = "APPROVE", seat_line: str = "") -> str:
        lines = [
            "### [2026-01-01T01:00:00Z] CLAUDE - REVIEW (verdict)",
            "Range: `%s`" % self.range_token(),
            "Verdict: %s" % verdict,
        ]
        if seat_line:
            lines.append(seat_line)
        return "\n".join(lines) + "\n"

    AUTHORIZED = "0801f97e-a603-4483-a871-dc6158d94c3e"
    HISTORICAL = "d14ba9b1-e602-4756-ba92-b9e1542bbcc6"
    UNAUTHORIZED = "9f75b735-3ab2-4c52-9bd4-7e576af859c1"

    def test_anchored_capture_reads_actor_and_kind_from_grammar(self) -> None:
        self.assertEqual(
            coordination_entry_actor_kind("### [2026-01-01T00:00:00Z] CODEX - HANDOFF (notes)"),
            ("CODEX", "HANDOFF"),
        )
        # En/em dash separators used by some composed headings must also parse.
        self.assertEqual(
            coordination_entry_actor_kind("### [2026-01-01T00:00:00Z] CLAUDE — REVIEW (notes)"),
            ("CLAUDE", "REVIEW"),
        )
        self.assertEqual(coordination_entry_actor_kind("not a heading"), ("", ""))

    def test_real_false_positive_heading_no_longer_matches_foreign_actor_kind(self) -> None:
        entry = self.entry(self.REAL_FALSE_POSITIVE_HEADING, ["Range: `%s`" % self.range_token()])
        self.assertFalse(
            coordination_entry_matches(entry, actor="CODEX", kind="REVIEW", range_token=self.range_token())
        )
        # It still matches what it actually declares.
        self.assertTrue(
            coordination_entry_matches(entry, actor="CLAUDE", kind="HANDOFF", range_token=self.range_token())
        )

    def test_approve_without_seat_guid_blocks_as_unattributed(self) -> None:
        result = self.run_validator(
            self.ledger(self.handoff_entry(), self.review_entry()),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_unattributed_verdict")

    def test_approve_with_unauthorized_seat_blocks(self) -> None:
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),
                self.review_entry(seat_line="Seat: session %s (registered)" % self.UNAUTHORIZED),
            ),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_unauthorized_seat")
        self.assertEqual(result["detail"]["seatSession"], self.UNAUTHORIZED)

    def test_approve_with_authorized_seat_releases(self) -> None:
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),
                self.review_entry(seat_line="Seat: registered 'claude' seat, session %s" % self.AUTHORIZED),
            ),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNone(result)

    def test_handoff_needs_no_seat_guid(self) -> None:
        # Phase 2 binds ONLY the decisive verdict entry; a GUID-less handoff still counts.
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),  # no Seat line anywhere in the handoff
                self.review_entry(seat_line="Seat: session %s" % self.AUTHORIZED),
            ),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNone(result)

    def test_blocking_verdict_blocks_even_without_seat_guid(self) -> None:
        # Fail-closed direction: identity never rescues a block.
        result = self.run_validator(
            self.ledger(self.handoff_entry(), self.review_entry(verdict="CHANGES_REQUESTED")),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_blocked")

    def test_no_allowlist_preserves_previous_behaviour(self) -> None:
        # Repos without authorizedReviewSessions keep the pre-identity semantics.
        result = self.run_validator(self.ledger(self.handoff_entry(), self.review_entry()))
        self.assertIsNone(result)

    def test_additional_handoff_actor_accepted_only_when_configured(self) -> None:
        fable_ledger = self.ledger(
            self.handoff_entry(actor="FABLE"),
            self.review_entry(seat_line="Seat: session %s" % self.AUTHORIZED),
        )
        blocked = self.run_validator(fable_ledger, authorizedReviewSessions=[self.AUTHORIZED])
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason"], "content_approval_missing_handoff")
        released = self.run_validator(
            fable_ledger,
            authorizedReviewSessions=[self.AUTHORIZED],
            additionalHandoffActors=["FABLE"],
        )
        self.assertIsNone(released)

    def test_landed_ci1_ledger_shape_still_releases(self) -> None:
        # Regression guard (483 [7](f)): the shape of the ALREADY-LANDED CI-1 ledger --
        # CODEX handoff, then a CLAUDE review whose Seat line names the historical
        # verdict author -- must still release under the new code with the allowlist
        # as committed (which deliberately retains that historical session).
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),
                self.review_entry(
                    seat_line=(
                        "Seat: registered 'claude' seat, session %s, seat-registry.json v17" % self.HISTORICAL
                    )
                ),
            ),
            authorizedReviewSessions=[self.AUTHORIZED, self.HISTORICAL],
        )
        self.assertIsNone(result)

    def test_malformed_heading_never_matches_actor_kind(self) -> None:
        # Requirement (M) (opus 487): a heading that does not parse under the grammar
        # must never match ANY actor/kind -- fail closed, not fall through to substring.
        for bad in (
            "### [2026-01-01T00:00:00Z] (no actor at all)",
            "### CODEX - REVIEW (missing timestamp bracket)",
            "### [2026-01-01T00:00:00Z] CODEX REVIEW (missing separator)",
        ):
            entry = self.entry(bad, ["Range: `%s`" % self.range_token(), "Verdict: APPROVE"])
            self.assertFalse(
                coordination_entry_matches(entry, actor="CLAUDE", kind="REVIEW", range_token=self.range_token()),
                bad,
            )

    def test_kind_axis_verdict_echoing_ack_does_not_release(self) -> None:
        # The KIND axis was the larger untested surface (235 headings, opus 487 [2]) and
        # this is the weaponizable shape: an ACK echoing a review verdict in prose AND
        # carrying the approve token. Under the old matcher it satisfies actor=CLAUDE
        # (prose 'Claude'), kind=REVIEW (prose 'review'), file-order-last, and releases.
        ack_echo = (
            "### [2026-01-01T02:00:00Z] CODEX - ACK (Claude review APPROVE received; proceeding to finalize)\n"
            "Range: `%s`\n" % self.range_token()
            + "Verdict: APPROVE\n"
        )
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),
                self.review_entry(verdict="CHANGES_REQUESTED", seat_line="Seat: session %s" % self.AUTHORIZED),
                ack_echo,
            ),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        # The genuine latest CLAUDE - REVIEW verdict is CHANGES_REQUESTED; the ACK echo
        # must not be taken as a later decisive review.
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_blocked")

    def test_seat_session_extraction(self) -> None:
        body = "Range: x\nSeat: registered 'claude' seat, session %s, re-statted\n" % self.AUTHORIZED
        self.assertEqual(coordination_entry_seat_session(body), self.AUTHORIZED)
        self.assertEqual(coordination_entry_seat_session("Range: x\nVerdict: APPROVE\n"), "")

    def test_seat_session_last_line_wins_over_quoted_seat(self) -> None:
        # B3: an entry quoting another entry's Seat line above its own footer must
        # attribute to its OWN (last) Seat line, not the quoted one.
        body = (
            "Quoting the prior verdict:\nSeat: session %s (quoted)\n" % self.UNAUTHORIZED
            + "My own footer follows.\nSeat: session %s\n" % self.AUTHORIZED
        )
        self.assertEqual(coordination_entry_seat_session(body), self.AUTHORIZED)

    def test_unparsable_superseding_block_fails_closed(self) -> None:
        # R1: a LATER blocking review whose heading does not parse (Markdown-decorated)
        # must not be silently dropped -- the range-naming unparsable entry blocks the
        # whole gate as content_approval_unparsable_heading instead of releasing the
        # stale APPROVE.
        for bad_heading in (
            "### [2026-01-01T02:00:00Z] **CLAUDE - REVIEW** (superseding block)",
            "### [2026-01-01T02:00:00Z] CLAUDE / REVIEW (superseding block)",
            "### [2026-01-01T02:00:00Z] `CLAUDE - REVIEW` (superseding block)",
        ):
            superseding = (
                bad_heading + "\n"
                + "Range: `%s`\n" % self.range_token()
                + "Verdict: CHANGES_REQUESTED\n"
                + "Seat: session %s\n" % self.AUTHORIZED
            )
            result = self.run_validator(
                self.ledger(
                    self.handoff_entry(),
                    self.review_entry(seat_line="Seat: session %s" % self.AUTHORIZED),
                    superseding,
                ),
                authorizedReviewSessions=[self.AUTHORIZED],
            )
            self.assertIsNotNone(result, bad_heading)
            self.assertEqual(result["reason"], "content_approval_unparsable_heading", bad_heading)
            self.assertEqual(
                result["expectedEntryFormat"]["requiredHeadingFormat"],
                "### [<timestamp>] ACTOR - KIND (free text)",
            )

    def test_unparsable_heading_for_other_range_is_ignored(self) -> None:
        # The unparsable check is scoped to entries NAMING this range, so historic
        # decorated headings about other ranges stay inert (no-op on the live file).
        other = "### [2026-01-01T02:00:00Z] **CLAUDE - REVIEW** (other range)\nRange: `%s..%s`\nVerdict: APPROVE\n" % ("a" * 40, "b" * 40)
        result = self.run_validator(
            self.ledger(
                self.handoff_entry(),
                self.review_entry(seat_line="Seat: session %s" % self.AUTHORIZED),
                other,
            ),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNone(result)

    def test_blocked_payload_documents_seat_requirement_and_grammar(self) -> None:
        # R2: the recovery guidance must describe the ACTUAL semantics -- parsed
        # grammar, not substring -- and must name the Seat requirement when the
        # allowlist is configured, so following it exactly cannot re-block.
        result = self.run_validator(
            self.ledger(self.handoff_entry()),
            authorizedReviewSessions=[self.AUTHORIZED],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "content_approval_missing")
        fmt = result["expectedEntryFormat"]
        self.assertEqual(fmt["requiredSeatLine"], "Seat: <one of contentReviewGate.authorizedReviewSessions>")
        self.assertEqual(fmt["requiredHeadingFormat"], "### [<timestamp>] ACTOR - KIND (free text)")
        self.assertNotIn("matched case-insensitively by substring", fmt["notes"])
        self.assertIn("PARSED", fmt["notes"])
        self.assertIn("Seat:", fmt["notes"])
        self.assertIn("Seat: <authorized GUID>", result["recoveryCommand"])


if __name__ == "__main__":
    unittest.main()


class StaleLockedWorktreePruneTests(unittest.TestCase):
    """A worktree whose creation was KILLED midway is locked with no directory behind it.

    `git worktree add` locks a new worktree with reason "initializing" and unlocks it on
    success. When the process dies first -- which the bounded closeout runner does
    deliberately on timeout or CPU stall -- the lock outlives the directory.
    `git worktree prune` skips locked worktrees BY DESIGN, so that entry can never clear
    itself while closeout keeps blocking on stale worktrees. Observed live on 2026-09-04:
    two such entries, with `git worktree prune -v` reporting nothing to do.
    """

    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="stale-locked-wt-"))
        self.addCleanup(shutil.rmtree, self.tempdir, ignore_errors=True)
        self.repo = self.tempdir / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "master")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("hello", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")

    def _admin_names(self) -> set:
        admin_root = self.repo / ".git" / "worktrees"
        return {item.name for item in admin_root.iterdir()} if admin_root.is_dir() else set()

    def _killed_mid_init(self, name: str) -> Path:
        """Reproduce the exact on-disk state: locked "initializing", directory gone."""
        path = self.tempdir / name
        git(self.repo, "worktree", "add", "-b", "br-" + name, str(path))
        git(self.repo, "worktree", "lock", "--reason", "initializing", str(path))
        shutil.rmtree(path)
        return path

    def test_plain_git_prune_CANNOT_clear_a_stale_locked_worktree(self) -> None:
        """Control probe. Without it the real test could pass for the wrong reason."""
        self._killed_mid_init("wt-a")
        before = self._admin_names()
        result = subprocess.run(
            ["git", "worktree", "prune", "-v"], cwd=str(self.repo), text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._admin_names(),
            before,
            "git worktree prune cleared a locked entry; the defect this test guards is gone",
        )

    def test_prune_worktrees_clears_it(self) -> None:
        self._killed_mid_init("wt-b")
        detected = stale_locked_worktree_admin_entries(self.repo)
        self.assertEqual(len(detected), 1, detected)
        self.assertEqual(detected[0]["lockReason"], "initializing")

        report = prune_worktrees(self.repo)
        self.assertEqual(report["pruneReturncode"], 0, report)
        self.assertEqual(len(report["staleLockedUnlocked"]), 1, report)
        self.assertEqual(self._admin_names(), set(), "stale locked entry survived prune_worktrees")
        self.assertEqual(stale_locked_worktree_admin_entries(self.repo), [])

    def test_a_LIVE_locked_worktree_is_never_unlocked(self) -> None:
        """The fix must stay narrow: a lock over a directory that EXISTS is left alone."""
        live = self.tempdir / "wt-live"
        git(self.repo, "worktree", "add", "-b", "br-live", str(live))
        git(self.repo, "worktree", "lock", "--reason", "held by another lane", str(live))

        self.assertEqual(stale_locked_worktree_admin_entries(self.repo), [])
        report = prune_worktrees(self.repo)
        self.assertEqual(report["staleLockedUnlocked"], [], report)
        self.assertTrue(live.is_dir())
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=str(self.repo), text=True, capture_output=True
        ).stdout
        self.assertIn("locked held by another lane", listing)
