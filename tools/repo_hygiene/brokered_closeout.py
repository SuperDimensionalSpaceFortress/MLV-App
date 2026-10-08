from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .core import (
    HygieneError,
    canonical_json,
    deep_merge,
    normalize_rel,
    resolve_repo_root,
    run_command,
    run_git,
    sha256_text,
    utc_now,
    write_json,
)


BROKER_SCHEMA_VERSION = "1.0"
CONFIG_PATH = Path("closeout.config.json")
DASHBOARD_MIN_INTERVAL_MS = 1000
DASHBOARD_MAX_INTERVAL_MS = 60000
HIGH_IMPACT_ACTIONS = [
    "clean_integrate",
    "checkpoint-owned-dirty",
    "push_target",
    "delete_local_branch",
    "delete_remote_branch",
    "worktree_cleanup",
    "snapshot_prune",
    "orphan_quarantine",
    "stash_promote",
    "repo_sweep_prune_merged",
    "split",
    "resolve_conflicts_with_agent",
    "preserve_dirty_cluster",
    "release_stale_claim",
    "remove_remediation_freeze",
]
REQUIRED_SCRIPT_NAMES = [
    "start-work-block.ps1",
    "work-block-complete.ps1",
    "detect-closeout.ps1",
    "repair-closeout.ps1",
    "publish-checkpoint.ps1",
    "finalize-closeout.ps1",
    "review-quorum.ps1",
    "orphan-quarantine.ps1",
    "audit-closeout.ps1",
    "repo-sweep-closeout.ps1",
    "remediate-retained-closeout.ps1",
    "agent-remediation-queue.ps1",
    "start-closeout-dashboard.ps1",
]
KNOWN_CLOSEOUT_FAILURE_TEXT = [
    "closeout gate failure",
    "review quorum failure",
    "review_quorum_failure",
    "review_quorum_missing",
    "review_quorum_blocked",
    "validation failure",
    "validation_failed",
    "baseline-dirty overlap",
    "baseline-dirty-overlaps-candidate",
    "stale refs",
    "stale_refs",
    "dirty ownership blocker",
    "owned_dirty_blocked",
    "unowned_dirty",
]
LEGACY_GENERIC_EVIDENCE_REPAIR_MESSAGES = {
    "brokered closeout evidence repair",
    "chore(closeout): repair closeout evidence artifacts",
}
LEGACY_GENERIC_CHECKPOINT_MESSAGES = {
    "brokered closeout checkpoint",
    "chore(closeout): checkpoint owned work",
}
GENERIC_CLOSEOUT_SUBJECT_PATTERNS = [
    re.compile(r"^chore\(closeout\): checkpoint .+ for [^ ]+$"),
    re.compile(r"^chore\(closeout\): repair .+ for [^ ]+ before .+$"),
    re.compile(r"^merge\(closeout\): integrate wb-[^ ]+ closeout hardening into .+$"),
]
BOUNDED_RUNNER_EXIT_CODE_DEFAULTS = {
    "timeout": 124,
    "outputCap": 125,
    "cpuStall": 126,
}
BOUNDED_RUNNER_STATUS_EXIT_CODE_KEYS = {
    "timeout": "timeout",
    "output_cap": "outputCap",
    "cpu_stall": "cpuStall",
}
POWERSHELL_DEFAULT_POLICY: Dict[str, Any] = {
    "preferredExecutable": "pwsh.exe",
    "fallbackExecutable": "powershell.exe",
    "requiredArgs": ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"],
    "preferFor": ["closeout adapters", "repo-state dashboard refresh", "repo-sweep recovery commands", "agent bridge process metadata probes"],
    "fallbackOnlyWhenPwshUnavailable": True,
    "windowsPowerShellOnly": [
        "tools/agent-bridge/codex_bridge_reminder.ps1 WinRT toast activation",
        "tools/agent-bridge/wake_codex.ps1 Windows shell activation",
        "tools/agent-bridge/watcher.py WinRT toast notification activation",
        "tools/agent-bridge/watcher.py WinForms balloon fallback",
    ],
    "justification": "PowerShell 7+ starts faster for repeated script launches; -NoProfile avoids profile load and profile side effects.",
}


def command_string(argv: Sequence[str]) -> str:
    parts: List[str] = []
    for raw in argv:
        value = str(raw)
        if not value:
            parts.append('""')
        elif any(ch.isspace() or ch == '"' for ch in value):
            parts.append('"' + value.replace('"', '\\"') + '"')
        else:
            parts.append(value)
    return " ".join(parts)


def summarize_commit_paths(paths: Sequence[str], *, max_items: int = 3) -> str:
    labels = [Path(str(path)).name or str(path) for path in paths if str(path)]
    if not labels:
        return "repo-managed updates"
    unique: List[str] = []
    for label in labels:
        if label not in unique:
            unique.append(label)
    if len(unique) == 1:
        return unique[0]
    if len(unique) <= max_items:
        return ", ".join(unique[:-1]) + ", and " + unique[-1]
    shown = unique[:max_items]
    return ", ".join(shown) + ", and %d more" % (len(unique) - max_items)


def evidence_repair_reason_label(reason: str) -> str:
    mapping = {
        "publish_missing_upstream": "publish with missing upstream metadata",
        "publish_ahead_only": "publish ahead-only branch state",
        "final_push": "final push",
    }
    return mapping.get(reason, reason.replace("_", " "))


def normalize_commit_subject(value: Optional[str]) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if text in LEGACY_GENERIC_EVIDENCE_REPAIR_MESSAGES or text in LEGACY_GENERIC_CHECKPOINT_MESSAGES:
        return ""
    if any(pattern.match(text) for pattern in GENERIC_CLOSEOUT_SUBJECT_PATTERNS):
        return ""
    return text


def closeout_commit_message_with_details(subject: str, details: Sequence[str]) -> str:
    clean_subject = normalize_commit_subject(subject)
    if not clean_subject:
        raise HygieneError("closeout commit subject must describe the user-visible work")
    clean_details = [str(detail).strip() for detail in details if str(detail).strip()]
    if not clean_details:
        return clean_subject
    return clean_subject + "\n\n" + "\n".join(clean_details)


def work_block_commit_subject(manifest: Optional[Dict[str, Any]]) -> str:
    if not isinstance(manifest, dict):
        return ""
    for key in ("commitSubject", "summary", "objective"):
        subject = normalize_commit_subject(manifest.get(key))
        if subject:
            return subject
    return ""


def nul_separated_paths(stdout: str) -> List[str]:
    """Paths from a git path-list command run with `-z`. Without `-z` git quotes a non-ASCII path
    (core.quotepath), so a prefix or equality test on the plain output misses it (bus de09cae)."""
    return [normalize_rel(part) for part in stdout.split("\0") if normalize_rel(part)]


def commit_changed_paths(repo_root: Path, commit_hash: str) -> List[str]:
    result = run_git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-z", "-r", commit_hash])
    if result.returncode != 0:
        return []
    return sorted(set(nul_separated_paths(result.stdout)))


def closeout_evidence_paths_only(paths: Sequence[str]) -> bool:
    normalized = [normalize_rel(path) for path in paths if normalize_rel(str(path))]
    return bool(normalized) and all(path.startswith(".closeout-evidence/") for path in normalized)


def delivered_work_commit_subject(repo_root: Path, detection: Dict[str, Any], manifest: Optional[Dict[str, Any]]) -> str:
    target_head = str(detection.get("targetHead") or "")
    feature_head = str(detection.get("featureHead") or "")
    if target_head and feature_head:
        result = run_git(repo_root, ["log", "--format=%H%x1f%s", f"{target_head}..{feature_head}"])
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "\x1f" not in line:
                    continue
                commit_hash, subject = line.split("\x1f", 1)
                if closeout_evidence_paths_only(commit_changed_paths(repo_root, commit_hash)):
                    continue
                clean_subject = normalize_commit_subject(subject)
                if clean_subject:
                    return clean_subject
    return work_block_commit_subject(manifest)


def evidence_repair_commit_message(
    config: Dict[str, Any],
    *,
    reason: str,
    work_block_id: str,
    paths: Sequence[str],
    work_summary: Optional[str] = None,
) -> str:
    configured = str(config.get("evidenceRepair", {}).get("commitMessage") or "").strip()
    generated_detail = "Closeout: repair %s before %s." % (
        summarize_commit_paths(paths),
        evidence_repair_reason_label(reason),
    )
    summary = normalize_commit_subject(work_summary) or normalize_commit_subject(configured)
    if summary:
        return closeout_commit_message_with_details(
            "evidence: %s" % summary,
            [generated_detail, "Work block: %s." % work_block_id],
        )
    return "chore(closeout): repair %s for %s before %s" % (
        summarize_commit_paths(paths),
        work_block_id,
        evidence_repair_reason_label(reason),
    )


def checkpoint_commit_message(
    work_block_id: str,
    paths: Sequence[str],
    *,
    explicit_message: Optional[str] = None,
    work_summary: Optional[str] = None,
) -> str:
    configured = normalize_commit_subject(explicit_message)
    summary = configured or normalize_commit_subject(work_summary)
    if summary:
        return closeout_commit_message_with_details(
            summary,
            ["Closeout: checkpoint %s." % summarize_commit_paths(paths), "Work block: %s." % work_block_id],
        )
    return "chore(closeout): checkpoint %s for %s" % (summarize_commit_paths(paths), work_block_id)


def dirty_split_commit_message(work_block_id: str, paths: Sequence[str]) -> str:
    return "chore(closeout): preserve split-owned changes for %s (%s)" % (
        work_block_id,
        summarize_commit_paths(paths),
    )


def closeout_merge_commit_message(branch: str, target_branch: str, *, work_summary: Optional[str] = None) -> str:
    if branch.startswith("codex/work-block/"):
        work_block_id = branch.rsplit("/", 1)[-1]
        fallback = "merge(closeout): integrate %s closeout hardening into %s" % (work_block_id, target_branch)
        summary = normalize_commit_subject(work_summary)
        if summary:
            return closeout_commit_message_with_details(
                "integration: %s" % summary,
                ["Closeout: integrate work block %s into %s after clean validation." % (work_block_id, target_branch)],
            )
        return fallback
    if branch.startswith("closeout/split/"):
        work_block_id = branch.rsplit("/", 1)[-1]
        return "merge(closeout): integrate preserved split changes from %s into %s" % (work_block_id, target_branch)
    return "merge(closeout): integrate %s into %s after clean validation" % (branch, target_branch)


def powershell_policy(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    configured = (config or {}).get("powerShell") if isinstance(config, dict) else None
    if isinstance(configured, dict):
        return deep_merge(POWERSHELL_DEFAULT_POLICY, configured)
    return deep_merge(POWERSHELL_DEFAULT_POLICY, {})


def powershell_executable_for_policy(policy: Dict[str, Any]) -> str:
    preferred = str(policy.get("preferredExecutable") or "pwsh.exe")
    if os.name != "nt" or not bool(policy.get("fallbackOnlyWhenPwshUnavailable", True)):
        return preferred
    probes = [preferred]
    if preferred.lower() == "pwsh.exe":
        probes.append("pwsh")
    for candidate in probes:
        if shutil.which(candidate):
            return candidate
    return str(policy.get("fallbackExecutable") or "powershell.exe")


def powershell_command_prefix(config: Optional[Dict[str, Any]] = None) -> List[str]:
    policy = powershell_policy(config)
    args = policy.get("requiredArgs") if isinstance(policy.get("requiredArgs"), list) else []
    return [powershell_executable_for_policy(policy)] + [str(arg) for arg in args]


def closeout_script_command(script: str, extra_args: Optional[Sequence[str]] = None, config: Optional[Dict[str, Any]] = None) -> str:
    argv = powershell_command_prefix(config) + ["-File", "tools\\closeout\\%s" % script, "-RepoRoot", "."]
    argv.extend(str(arg) for arg in (extra_args or []))
    return command_string(argv)


def command_references_closeout_script(command: str, script: str) -> bool:
    normalized = str(command or "").replace("/", "\\").casefold()
    return ("tools\\closeout\\%s" % script).casefold() in normalized


def effective_closeout_script_command(
    config: Dict[str, Any],
    script: str,
    extra_args: Optional[Sequence[str]] = None,
    configured: Optional[str] = None,
) -> str:
    configured_text = str(configured or "").strip()
    if not configured_text or command_references_closeout_script(configured_text, script):
        return closeout_script_command(script, extra_args, config)
    raise HygieneError(
        "unsupported configured closeout command for %s; dashboard and closeout command surfaces "
        "may only resolve to repo-owned tools\\closeout\\%s" % (script, script)
    )


DEFAULT_CLOSEOUT_CONFIG: Dict[str, Any] = {
    "schemaVersion": BROKER_SCHEMA_VERSION,
    "policyVersion": "2026-05-05.brokered-closeout",
    "stateRoot": ".claude-state/closeout",
    "git": {
        "targetBranch": "master",
        "remote": "fork",
        "allowLocalOnly": True,
        "protectedBranches": ["master", "main", "develop", "release/*", "closeout/recovery/detached/*"],
        "featureBranchPatterns": ["codex/*", "claude/*", "hygiene/*", "work/*", "feature/*"],
        "fetchBeforeEvidence": True,
    },
    "workBlockBootstrap": {
        "autoBranchFromProtectedTarget": True,
        "branchPrefix": "codex/work-block",
        "requireIntegratedStartHeadForFinalize": True,
    },
    "contentReviewGate": {
        "requireClaudeApprovalForFinalize": False,
        "coordinationFile": ".claude-state/coordination/gpu-lane-impl-review-sync.md",
        "handoffActor": "CODEX",
        "handoffKind": "HANDOFF",
        "reviewActor": "CLAUDE",
        "reviewKind": "REVIEW",
        "approveTokens": ["APPROVE"],
        "blockingTokens": ["CHANGES_REQUESTED", "BLOCKER"],
        "requireHandoff": True,
        "authorizedReviewSessions": ["5fc3fc6e-345f-40b8-bb3d-7abd6302b459"],
    },
    "validation": {
        "timeoutMs": 120000,
        "maxOutputBytes": 8388608,
        "fullSuiteEnvVar": "CLOSEOUT_RUN_FULL_VALIDATION",
        "runFullSuiteByDefault": False,
        "commands": [
            {
                "name": "brokered-closeout-smoke-core",
                "argv": [
                    "py",
                    "-3",
                    "-m",
                    "unittest",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_timeout_and_output_cap_settings_are_contract_required",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_bounded_runner_exit_code_taxonomy_is_contract_required",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_validation_resource_policy_sets_below_normal_priority_and_affinity",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_bounded_runner_cpu_watchdog_terminates_hot_silent_child",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_full_validation_suite_is_skipped_without_explicit_request",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_path_scoped_validation_skips_unmatched_commands",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_validation_commands_ignore_failure_words_in_successful_test_names",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_powershell_policy_prefers_pwsh_no_profile_for_closeout_commands",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_tooling_stale_reports_missing_power_shell_policy",
                    "-v",
                ],
                "pathPatterns": ["closeout.config.json", "tools/closeout/**", "tools/repo_hygiene/**", "tools/repo-hygiene/**"],
            },
            {
                "name": "brokered-closeout-smoke-state",
                "argv": [
                    "py",
                    "-3",
                    "-m",
                    "unittest",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_state_snapshot_writes_dashboard_ready_ledger_and_audit",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_state_snapshot_reports_worktree_inspection",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_state_latest_only_refresh_updates_feed_without_audit_noise",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_state_rollback_readiness_fails_closed_without_actor",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_validate_rollback_manifest_accepts_immutable_history_and_audit",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_validate_rollback_manifest_rejects_latest_forbidden_and_stale_evidence",
                    "-v",
                ],
                "pathPatterns": ["closeout.config.json", "tools/closeout/**", "tools/repo_hygiene/**", "tools/repo-hygiene/**"],
            },
            {
                "name": "brokered-closeout-smoke-dashboard",
                "argv": [
                    "py",
                    "-3",
                    "-m",
                    "unittest",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_state_dashboard_and_rollback_contract_required",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_actions_are_read_only_and_owned",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_action_request_writes_packet_without_mutation",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_action_request_rejects_stale_or_unknown_action",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_action_request_rejects_missing_future_or_malformed_helper_evidence",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_action_request_rejects_empty_tuple_values_and_path_escape",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_page_uses_sse_with_polling_fallback",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_closeout_dashboard_page_preserves_configured_client_state_keys",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_dashboard_refresh_command_rejects_unsupported_configured_command",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_closed_postcondition_blocks_linked_sibling_worktree",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_closed_postcondition_does_not_treat_primary_root_as_stale_sibling",
                    "tools.repo_hygiene.test_brokered_closeout.BrokeredCloseoutTests.test_repo_closed_postcondition_still_blocks_genuine_stale_detached_sibling",
                    "-v",
                ],
                "pathPatterns": ["closeout.config.json", "tools/closeout/**", "tools/repo_hygiene/**", "tools/repo-hygiene/**"],
            },
            {
                "name": "brokered-closeout-full",
                "argv": ["py", "-3", "-m", "unittest", "tools.repo_hygiene.test_brokered_closeout", "-v"],
                "runMode": "full",
                "timeoutMs": 600000,
                "pathPatterns": ["closeout.config.json", "tools/closeout/**", "tools/repo_hygiene/**", "tools/repo-hygiene/**"],
            },
            {
                "name": "agent-bridge-wmi-regression",
                "argv": [
                    "py",
                    "-3",
                    "-m",
                    "unittest",
                    "tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_mcp_server_watchdog_throttles_wrapper_identity_checks",
                    "tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_bootstrap_watcher_enumeration_prefers_pwsh_for_cim",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_queries_prefer_pwsh_for_cim",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_uses_pwsh_alias_when_exe_probe_is_absent",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_queries_fall_back_to_windows_powershell",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_trampoline_host_env_queries_prefer_pwsh_for_cim",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_server_wrapper_process_entry_prefers_pwsh_for_cim",
                    "tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_supervisor_throttles_host_identity_revalidation",
                ],
                "pathPatterns": ["tools/agent-bridge/**", "AGENTS.md", "CLAUDE.md", "bridge_trigger_heuristics.md"],
            },
            {
                "name": "repo-hygiene-policy",
                "argv": ["py", "-3", "tools/repo-hygiene/hygiene.py", "--repo-root", ".", "verify-policy"],
            },
        ]
    },
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
    "powerShell": POWERSHELL_DEFAULT_POLICY,
    "paths": {
        "generated": [
            ".claude-state/**",
            ".codex-state/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/build*/**",
            "platform/**/build*/**",
            ".claude-state/closeout-log/**",
        ],
        "sensitive": [".claude/**", ".claude", ".git/**", ".git"],
        "state": ".claude-state/closeout",
    },
    "dirty": {
        "unclaimedOutsideDelta": "foreign",
        "sensitiveUnownedBlocks": True,
        "autoClaimCleanAtStart": True,
        "cleanAtStartSensitiveAutoClaim": [
            ".claude/analysis/*.md",
            ".claude/ANALYSIS_LOG.md",
        ],
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
    "stashPolicy": {
        "allowAutoStash": False,
        "allowForeignDirtyStash": False,
        "allowOwnedDirtyCheckpoint": True,
    },
    "cleanupPolicy": {
        "deleteLocalBranchAfterSuccess": True,
        "deleteRemoteBranchAfterSuccess": False,
        "pruneSnapshotsAfterSuccess": True,
        "removeIntegrationWorktreeAfterFailure": True,
        "retainOriginalWorktreeOnTreeMismatch": True,
        "removeCleanDetachedWorktreesInSweep": False,
        "dropStashesInSweep": False,
        "protectedWorktreeRoots": [
            ".claude/worktrees/**",
            "**/.claude-state/closeout/repo-sweep/detached-preserve/**",
        ],
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
        "protectedTargetNoopCloseout": {
            "enabled": True,
        },
        "unifiedTruthReport": {
            "enabled": True,
            "authoritativeSource": "repoClosedPostcondition.closeoutCleanTruth",
            "reportFields": ["rawGit", "policy", "cleanup"],
        },
        "exemptStatusPatterns": [
            ".claude-state/**",
            ".codex-state/**",
            "**/__pycache__/**",
            "**/*.pyc",
            ".claude-state/closeout-log/**",
        ],
    },
    "runtimeServices": {
        "mlvapp-preview": {
            "enabled": False,
            "stopBeforePromotion": True,
            "restartAfterCleanPromotion": False,
            "statusCommand": [],
            "stopCommand": [],
            "startCommand": [],
        },
    },
    "repair": {
        "autoPublishFeatureBranch": True,
        "allowedPublishRemotes": ["fork", "origin"],
        "setUpstreamOnPublish": True,
    },
    "locking": {
        "detectTimeoutMs": 120000,
        "finalizeTimeoutMs": 600000,
        "repoSweepTimeoutMs": 600000,
        "adapterHeartbeatSeconds": 30,
        "maxProcessOutputBytes": 8388608,
        "boundedRunnerExitCodes": BOUNDED_RUNNER_EXIT_CODE_DEFAULTS,
        "failureTextPatterns": KNOWN_CLOSEOUT_FAILURE_TEXT,
    },
    "autoEligibilityRepair": {
        "timeoutMs": 300000,
    },
    "repoStateLedger": {
        "enabled": True,
        "artifactSchema": "repo-state-snapshot.v1",
        "artifactKind": "repoStateSnapshot",
        "auditType": "repo_state_snapshot",
        "latestPath": ".claude-state/closeout/repo-state/latest.json",
        "historyRoot": ".claude-state/closeout/repo-state/history",
        "historyRetentionDays": 90,
        "closeoutHistoryLimit": 25,
        "closeoutHistorySchema": "closeout-history-index.v1",
        "liveRefreshWritesHistory": False,
        "refreshCommandPolicy": "repo-owned-write-repo-state-latest-only",
        "liveRefreshCommand": closeout_script_command("write-repo-state.ps1", ["-Write", "-LatestOnly"]),
        "feedPathPolicy": {
            "generatedRoot": ".claude-state/closeout/repo-state",
            "latestJsonIsMutableDashboardFeed": True,
            "latestJsonIsRollbackEvidence": False,
            "historyJsonIsImmutableRollbackEvidence": True,
            "latestFeedUse": "display-only-dashboard-feed",
            "historyEvidenceUse": "immutable-history-source-for-rollback-actor",
        },
        "include": ["branch", "tracking", "dirty", "worktrees", "stashes", "localBranches", "closeoutCleanTruth", "auditTrail", "closeoutHistory", "rollback"],
    },
    "webDashboardSpec": {
        "enabled": True,
        "localUrl": "http://127.0.0.1:8765/closeout",
        "stickyUrlPath": "/closeout",
        "refreshTransport": "sse-with-polling-fallback",
        "autoRefreshMs": 5000,
        "refreshCommand": closeout_script_command("write-repo-state.ps1", ["-Write", "-LatestOnly"]),
        "refreshCommandPolicy": "repo-owned-write-repo-state-latest-only",
        "readOnlyByDefault": True,
        "preserveClientStateAcrossRefresh": True,
        "preservedClientStateKeys": ["scrollPosition", "focusedElement", "selectedWorkBlockId", "expandedRows", "activeHistoryFilters"],
        "mutationModel": "symbolic-action-request-only",
        "actionRequestRoot": ".claude-state/closeout/dashboard-action-requests",
        "feedAuthority": "latest-json-is-display-feed-only",
        "duplicateLaunchPolicy": "reuse-same-repo-fail-foreign-owner",
        "rollbackForbiddenActions": ["reset-hard", "force-push", "delete-evidence"],
        "helper": {
            "scriptPath": "tools\\closeout\\start-closeout-dashboard.ps1",
            "module": "tools.repo_hygiene.closeout_dashboard",
            "host": "127.0.0.1",
            "port": 8765,
            "reuseExistingForSameRepo": True,
            "serverProcessIdSource": "/api/closeout/actions",
            "readinessEndpoint": "/api/closeout/actions",
            "staleAfterMs": 15000,
        },
        "endpoints": {
            "page": "/closeout",
            "latest": "/api/closeout/repo-state/latest",
            "historyIndex": "/api/closeout/repo-state/history-index",
            "historySnapshot": "/api/closeout/repo-state/history/{snapshotId}",
            "actions": "/api/closeout/actions",
            "actionsPreview": "/api/closeout/actions/preview",
            "actionsRequest": "/api/closeout/actions/request",
            "actionsRequestHistory": "/api/closeout/actions/requests",
            "events": "/api/closeout/events",
        },
        "dataSources": [
            "repoStateLedger.latestPath",
            "repoClosedPostcondition.closeoutCleanTruth",
            "closeoutAudits",
            "workBlockManifests",
            "repoSweepCandidateReports",
        ],
        "views": ["repo-state", "closeout-workflow", "workflow-comparison", "historical-closeouts", "rollback-readiness"],
        "primaryPanels": ["repo-map", "workflow-lane", "workflow-comparison", "blocker-queue", "action-preview", "action-request-history", "audit-timeline", "rollback-readiness"],
    },
    "rollbackPolicy": {
        "enabled": True,
        "preferredStrategy": "git-revert-or-recovery-branch",
        "allowedStrategies": ["git-revert", "recovery-branch-restore", "preservation-ref-promote", "path-restore-from-snapshot"],
        "startNewWorkBlockForRollback": True,
        "requireUserApprovalForRollback": True,
        "neverUseResetHardWithoutExplicitUserRequest": True,
        "requireRepoStateSnapshotBeforeMutation": True,
        "requireRecoveryCommandInMutatingAudits": True,
        "writeRollbackPlanBeforeMutation": True,
        "readinessSchema": "rollback-readiness.v1",
        "requiredManifestSchema": "closeout-rollback-manifest.v1",
        "validationSchema": "closeout-rollback-manifest-validation.v1",
        "manifestRoot": ".claude-state/closeout/rollback",
        "validatorCommand": "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tools\\closeout\\validate-rollback-manifest.ps1 -RepoRoot . -ManifestPath <path>",
        "readinessDefaultActionability": "read-only-no-actor",
        "validatorActionability": "read-only-validator",
        "latestFeedIsRollbackEvidence": False,
        "requireImmutableSourceSnapshotForRollback": True,
        "requiredManifestFields": [
            "schema",
            "targetHead",
            "sourceSnapshotPath",
            "sourceSnapshotHash",
            "sourceSnapshotAuditHash",
            "repoClosedAuditHash",
            "policyHash",
            "plannedStrategy",
            "userApproval",
            "recoveryCommand",
        ],
        "disallowedDefaultActions": ["reset-hard", "force-push", "delete-evidence"],
        "recoveryEvidenceRoots": [
            ".claude-state/closeout/manual-prune",
            ".claude-state/closeout/dirty-splits",
            ".closeout-evidence",
        ],
        "userFacingFeasibility": "high for committed Git changes; medium for branch/worktree cleanup with recovery evidence; manual for uncommitted foreign dirty paths",
    },
    "closeoutAddendumPersistence": {
        "enabled": True,
        "sameTurnRequired": True,
        "incomingLabels": ["incoming addendum", "closeout addendum", "closeout rule", "closeout blocker addendum"],
        "repoWideSurfaces": ["AGENTS.md", "CLAUDE.md", "closeout.config.json", "tools/repo_hygiene/brokered_closeout.py", "tools/repo_hygiene/test_brokered_closeout.py"],
        "minimumDurableArtifacts": ["agent_rule", "broker_policy", "test_or_tooling_baseline_guard"],
    },
    "finalizeLoop": {
        "enabled": True,
        "maxRetries": 8,
        "allowRepeatedBlockerEvidenceTupleRenewal": False,
        "safeSecondOrderRepairs": {
            "final_push_evidence_repaired": "evidence_repair",
            "target_push_rerun_required": "target_push_recovery",
            "stale_review": "renew_stale_review",
            "validation_failed": "rerun_validation_smoke",
        },
        "retryAuditType": "finalize_retry",
    },
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
        "blockedLifecycleActions": [
            "broker-bootstrap",
            "start-work-block",
            "ensure-feature-branch",
            "publish",
            "finalize",
            "auto-closeout-hook",
            "pre-commit",
            "pre-push",
            "response-hook",
            "final-hook",
        ],
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
    "toolingBaseline": {
        "enabled": False,
        "baselineRef": None,
        "autoUpdate": True,
        "paths": [
            "AGENTS.md",
            "CLAUDE.md",
            "closeout.config.json",
            "tools/testing/test-ultra-magnus-agent-timeout.ps1",
            "tools/repo_hygiene/brokered_closeout.py",
            "tools/repo_hygiene/candidate_acceptance.py",
            "tools/repo_hygiene/test_candidate_acceptance.py",
            "tools/repo_hygiene/vendored_native_payloads.py",
            "tools/repo_hygiene/test_vendored_native_payloads.py",
            "tools/gates/payload-provenance-promotion-receipt.schema.json",
            "tools/gates/payload-provenance-promotion-claims.json",
            "tools/factory/capture-github-acceptance.ps1",
            "tools/repo_hygiene/work_block_cli.py",
            "tools/repo_hygiene/test_brokered_closeout.py",
            "tools/closeout/Invoke-CloseoutCli.ps1",
            "tools/closeout/write-repo-state.ps1",
            "tools/closeout/start-closeout-dashboard.ps1",
            "tools/closeout/work-block-complete.ps1",
            "tools/closeout/agent-remediation-queue.ps1",
            "tools/agent-bridge/codex_pre_response.ps1",
            "tools/agent-bridge/codex_pre_final.ps1",
            "tools/agent-bridge/codex_bridge_reminder.ps1",
            "tools/repo-hygiene/closeout.contract.json",
            "tools/repo-hygiene/hygiene.config.json",
        ],
        "requiredConfigKeys": [
            "git",
            "workBlockBootstrap",
            "workBlockBootstrap.requireIntegratedStartHeadForFinalize",
            "contentReviewGate",
            "contentReviewGate.requireClaudeApprovalForFinalize",
            "contentReviewGate.coordinationFile",
            "contentReviewGate.handoffActor",
            "contentReviewGate.reviewActor",
            "contentReviewGate.authorizedReviewSessions",
            "validation",
            "processResources",
            "powerShell",
            "powerShell.preferredExecutable",
            "powerShell.fallbackExecutable",
            "powerShell.requiredArgs",
            "powerShell.preferFor",
            "powerShell.fallbackOnlyWhenPwshUnavailable",
            "powerShell.windowsPowerShellOnly",
            "powerShell.justification",
            "paths",
            "dirty.cleanAtStartSensitiveAutoClaim",
            "dirtySplit",
            "toolingBaseline",
            "evidenceRepair",
            "stashPolicy",
            "cleanupPolicy",
            "reviewQuorum",
            "responseHookLifecycle",
            "hardClean",
            "runtimeServices",
            "blockerAutoRemediation",
            "repoSweep.maxNewInvestigationsPerRun",
            "candidateAcceptance",
            "candidateAcceptance.schema",
            "candidateAcceptance.enabled",
            "candidateAcceptance.providerRepository",
            "candidateAcceptance.requiredSurfaces",
            "candidateAcceptance.batchUntilAllSurfacesTerminal",
            "candidateAcceptance.carryApprovalsAcrossCandidateTuples",
            "candidateAcceptance.agentApprovalsGrantHumanAuthority",
            "candidateAcceptance.requireReadyForFinalize",
            "toolingBaseline.enabled",
            "toolingBaseline.requiredTestFiles",
            "closeoutAddendumPersistence",
            "finalizeLoop",
            "remediationFreeze",
            "agentRemediation",
            "agentRemediationQueue",
            "locking",
            "autoEligibilityRepair",
            "repoStateLedger",
            "webDashboardSpec",
            "webDashboardSpec.readOnlyByDefault",
            "webDashboardSpec.preserveClientStateAcrossRefresh",
            "webDashboardSpec.rollbackForbiddenActions",
            "rollbackPolicy",
            "rollbackPolicy.validationSchema",
            "rollbackPolicy.manifestRoot",
            "rollbackPolicy.validatorCommand",
            "rollbackPolicy.validatorActionability",
        ],
        "requiredHighImpactActions": ["clean_integrate", "checkpoint-owned-dirty", "delete_local_branch", "delete_remote_branch", "repo_sweep_prune_merged", "split", "resolve_conflicts_with_agent", "preserve_dirty_cluster", "release_stale_claim", "remove_remediation_freeze"],
        "requiredAutoQuorumActions": [
            "integrated_branch_prune",
            "integrated_remote_feature_prune",
            "patch_equivalent_remote_feature_prune",
            "remote_feature_clean_integrate",
            "repo_sweep_clean_integrate",
            "owned_dirty_checkpoint",
            "dirty_split",
            "foreign_dirty_integrated_branch_prune",
            "detached_dirty_preserve",
            "generated_only_detached_worktree_prune",
            "redundant_branch_prune",
            "explicit_protected_worktree_cleanup",
            "dirty_cluster_preservation",
            "stale_claim_remediation",
            "remediation_freeze_removal",
        ],
        "requiredTests": [
            "test_content_review_authorized_session_rotation_is_strict_singleton",
            "test_remediation_freeze_blocks_broker_bootstrap_lease_refresh_start_publish_finalize_and_hooks",
            "test_start_work_block_auto_branches_from_clean_protected_target",
            "test_start_work_block_blocks_dirty_protected_target_before_auto_branch",
            "test_finalize_blocks_work_block_started_from_unintegrated_base",
            "test_finalize_blocks_review_gated_chunk_without_claude_approval",
            "test_finalize_rejects_claude_approval_for_different_range",
            "test_remediation_freeze_environment_is_process_scoped_and_fresh_preservation_worktree_is_exempt",
            "test_remediation_freeze_audit_packets_are_generated_exempt_and_content_addressed",
            "test_already_integrated_dirty_baseline_overlap_enters_remediation_triage",
            "test_remediation_packet_requires_stale_claim_proof_not_gone_upstream_only",
            "test_remediation_preservation_requires_exact_allowlist_and_ref_or_bundle_backing",
            "test_remediation_freeze_removal_requires_coordinator_lock_quorum_and_revalidation",
            "test_remediation_hard_clean_blocks_remaining_freeze_marker",
            "test_remediation_freeze_remove_blocks_fresh_dirty_marker",
            "test_remediation_freeze_remove_allows_stale_dirty_marker_after_quorum",
            "test_remediation_freeze_remove_allows_wrong_work_block_marker_after_quorum",
            "test_dirty_split_auto_remediates_owned_dirty_and_retains_foreign",
            "test_dirty_split_stale_tuple_is_rejected_before_mutation",
            "test_repo_sweep_foreign_dirty_integrated_branch_switches_and_prunes",
            "test_repo_sweep_foreign_dirty_integrated_branch_detaches_linked_worktree_and_prunes",
            "test_repo_sweep_foreign_dirty_target_overlap_blocks_with_exact_path_evidence",
            "test_repo_sweep_detached_dirty_worktree_is_preserved_before_cleanup",
            "test_repo_sweep_generated_only_detached_dirty_worktree_is_pruned_with_proof",
            "test_repo_sweep_large_real_detached_evidence_is_preserved_not_count_blocked",
            "test_detached_dirty_preserve_copy_uses_extended_length_destination",
            "test_repo_sweep_detached_dirty_preservation_refuses_stale_or_missing_commit_before_cleanup",
            "test_repo_sweep_explicit_protected_stale_worktree_cleanup_requires_exact_policy",
            "test_repo_sweep_protected_locked_worktree_without_exact_policy_is_inspect_only",
            "test_repo_sweep_merge_failed_report_has_agent_resolution_packet",
            "test_finalize_auto_quorum_renews_stale_review_when_policy_allows",
            "test_stale_review_tuple_blocks_when_target_moves",
            "test_repo_sweep_patch_equivalent_non_backup_branch_auto_quorum_prunes",
            "test_repo_sweep_retained_terminal_outcomes_prove_remediation_attempted_or_excluded",
            "test_pre_response_broker_bootstrap_records_dirty_baseline_without_worktree",
            "test_clean_at_start_new_dirty_paths_auto_claimed_and_checkpointed_through_quorum",
            "test_existing_analysis_note_clean_at_start_is_auto_claimed_despite_sensitive_root",
            "test_new_analysis_note_under_sensitive_root_still_blocks_as_unowned",
            "test_stale_integrated_claims_do_not_foreignize_clean_at_start_dirty",
            "test_baseline_dirty_claimed_path_blocks_as_mixed_and_not_checkpointed",
            "test_baseline_dirty_changed_after_start_blocks_as_mixed_without_claim",
            "test_repo_sweep_baseline_dirty_changed_is_not_pruned_as_foreign_dirty",
            "test_owned_dirty_checkpoint_stages_only_exact_owned_paths",
            "test_foreign_dirty_remains_retained_audited_and_does_not_block_independent_closeout",
            "test_completion_without_explicit_work_block_id_reports_deterministic_selection_reason",
            "test_clean_integration_worktree_add_uses_longpaths_config",
            "test_closeout_tooling_stale_blocks_before_hygiene_blocker",
            "test_tooling_baseline_check_is_plan_only_during_finalize",
            "test_missing_evidence_is_generated_and_committed_before_publish",
            "test_existing_evidence_is_refreshed_after_a_later_source_commit",
            "test_target_push_non_fast_forward_fetches_updates_local_target_and_reports_rerun",
            "test_finalize_loop_stops_on_repeated_identical_blocker_evidence_tuple",
            "test_finalize_loop_continues_when_evidence_repair_changes_tuple_and_pins_match",
            "test_generated_closeout_conflict_packets_are_not_owned_dirty",
            "test_remediate_retained_actor_applies_one_candidate_per_run",
            "test_repo_sweep_remote_integrated_feature_branch_is_pruned",
            "test_repo_sweep_remote_patch_equivalent_feature_branch_is_pruned",
            "test_repo_sweep_remote_unique_feature_branch_clean_integrates_and_prunes",
            "test_repo_sweep_remote_conflicting_feature_branch_writes_investigation_packet",
            "test_repo_sweep_merge_failed_promotes_agent_conflict_dispatch",
            "test_repo_sweep_agent_conflict_dispatch_writes_queue_packet",
            "test_agent_conflict_dispatch_shards_large_conflict_sets",
            "test_agent_remediation_queue_policy_fields_are_contract_required",
            "test_codex_agent_queue_consumer_plans_one_background_agent_per_eligible_shard",
            "test_agent_queue_surface_unavailable_writes_result_packets",
            "test_agent_queue_stale_packets_rejected_on_policy_hash_or_dirty_state_change",
            "test_agent_queue_dirty_hash_detects_same_status_byte_change",
            "test_agent_queue_stale_packet_blocks_even_when_valid_shard_can_spawn",
            "test_agent_queue_remote_fetch_failure_blocks_stale_check",
            "test_agent_queue_result_path_outside_result_root_is_stale",
            "test_agent_queue_skips_completed_result_paths_before_planning_more_shards",
            "test_agent_queue_retires_stale_plan_absent_packet_with_valid_proof",
            "test_agent_queue_retirement_remote_fetch_failure_blocks_without_retiring_packet",
            "test_agent_result_collection_rejects_out_of_scope_changed_paths",
            "test_agent_result_collection_returns_symbolic_next_action_without_mutation",
            "test_agent_result_collection_blocks_agent_reported_blockers_and_failed_validation",
            "test_agent_result_collection_rejects_missing_required_result_fields",
            "test_agent_result_collection_rejects_wrong_shard_identity",
            "test_repo_closed_postcondition_blocks_pending_agent_remediation_queue",
            "test_repo_closed_postcondition_blocks_unreadable_or_unretired_queue_artifacts",
            "test_repo_closed_postcondition_blocks_invalid_queue_retirement_proof",
            "test_repo_closed_postcondition_accepts_valid_queue_retirement_proof",
            "test_repo_closed_postcondition_rejects_wrong_result_tuple",
            "test_repo_closed_postcondition_rejects_stale_collection_retirement_tuple",
            "test_non_ancestor_historical_branch_prune_requires_bundle_backed_recovery",
            "test_dirty_detached_worktree_removal_refuses_missing_byte_preservation",
            "test_recovery_audit_records_heads_hashes_and_reviewer_verdicts",
            "test_stale_transaction_branch_pruned_after_recovery_evidence",
            "test_final_repo_sweep_after_prune_reports_zero_candidates",
            "test_deletion_tuple_rejects_missing_or_stale_recovery_artifact",
            "test_bounded_runner_kills_hung_finalize_child_with_descendants",
            "test_bounded_runner_direct_finalize_and_completion_share_timeout_policy",
            "test_bounded_runner_caps_oversized_child_output",
            "test_bounded_runner_normalizes_known_failure_text_with_zero_exit",
            "test_bounded_runner_trusts_finalize_semantic_success_over_validation_text",
            "test_bounded_runner_closeout_gate_failure_cannot_report_finalized",
            "test_bounded_runner_review_quorum_failure_requires_approval_artifact_and_rerun",
            "test_bounded_runner_timeout_leaves_no_orphan_child_processes",
            "test_validation_commands_are_bounded_and_kill_descendants",
            "test_validation_resource_policy_sets_below_normal_priority_and_affinity",
            "test_bounded_runner_cpu_watchdog_terminates_hot_silent_child",
            "test_closeout_adapter_heartbeat_is_contract_required",
            "test_full_validation_suite_is_skipped_without_explicit_request",
            "test_path_scoped_validation_skips_unmatched_commands",
            "test_validation_commands_ignore_failure_words_in_successful_test_names",
            "test_timeout_and_output_cap_settings_are_contract_required",
            "test_bounded_runner_exit_code_taxonomy_is_contract_required",
            "test_powershell_policy_prefers_pwsh_no_profile_for_closeout_commands",
            "test_repo_state_snapshot_writes_dashboard_ready_ledger_and_audit",
            "test_repo_state_snapshot_reports_worktree_inspection",
            "test_repo_state_dashboard_and_rollback_contract_required",
            "test_repo_state_latest_only_refresh_updates_feed_without_audit_noise",
            "test_repo_state_rollback_readiness_fails_closed_without_actor",
            "test_validate_rollback_manifest_accepts_immutable_history_and_audit",
            "test_validate_rollback_manifest_rejects_latest_forbidden_and_stale_evidence",
            "test_closeout_dashboard_actions_are_read_only_and_owned",
            "test_closeout_dashboard_action_request_writes_packet_without_mutation",
            "test_closeout_dashboard_action_request_rejects_stale_or_unknown_action",
            "test_closeout_dashboard_action_request_rejects_missing_future_or_malformed_helper_evidence",
            "test_closeout_dashboard_action_request_rejects_empty_tuple_values_and_path_escape",
            "test_closeout_dashboard_action_request_history_lists_recent_requests",
            "test_closeout_dashboard_action_request_history_surfaces_malformed_and_truncated_rows",
            "test_closeout_dashboard_rejects_malformed_config_numbers",
            "test_closeout_dashboard_history_snapshot_rejects_path_escape",
            "test_closeout_dashboard_page_uses_sse_with_polling_fallback",
            "test_closeout_dashboard_page_preserves_configured_client_state_keys",
            "test_dashboard_refresh_command_rejects_unsupported_configured_command",
            "test_stale_tooling_without_bounded_runner_reports_tooling_drift",
            "test_hard_clean_final_response_blocks_non_exempt_dirty_files",
            "test_hard_clean_final_response_blocks_remaining_stash",
            "test_hard_clean_final_response_passes_after_clean_promotion",
            "test_complete_finalize_enforces_hard_clean_config_without_switch",
            "test_work_block_complete_wrapper_finalizes_by_default",
            "test_complete_finalize_on_clean_protected_target_is_noop_repo_closed",
            "test_dirty_protected_target_finalize_recovers_to_feature_branch",
            "test_dirty_protected_target_recovery_owns_paths_despite_stale_claim",
            "test_repo_closed_postcondition_reports_unified_closeout_clean_truth",
            "test_repo_closed_postcondition_blocks_linked_sibling_worktree",
            "test_repo_closed_postcondition_does_not_treat_primary_root_as_stale_sibling",
            "test_repo_closed_postcondition_still_blocks_genuine_stale_detached_sibling",
            "test_closeout_clean_truth_preserves_raw_git_dirty_but_policy_clean_for_exempt_state",
            "test_closeout_clean_truth_contract_required",
            "test_hard_clean_blocks_retained_remote_feature_refs",
            "test_review_quorum_requires_allowed_ten_score_self_plus_two_independent",
            "test_declared_review_surfaces_write_unavailable_reports",
            "test_runtime_service_stops_before_validation_and_restarts_after_repo_closed",
            "test_runtime_service_not_restarted_after_failed_validation_stale_refs_or_repo_closed_failure",
            "test_closeout_tooling_stale_reports_missing_hard_clean_gate",
            "test_closeout_tooling_stale_reports_missing_power_shell_policy",
        ],
        "requiredTestFiles": [
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_acceptance_uses_two_phase_target_pinned_activation"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_tracked_and_default_active_policy_and_tooling_guards_stay_in_parity"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_live_provider_query_ignores_candidate_and_path_executable_shadowing"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_live_provider_system_curl_identity_must_stay_stable_during_query"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_live_provider_system_curl_rejects_redirect_status"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_live_provider_system_curl_rejects_user_writable_or_unsigned_client"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_windows_system_curl_trust_classifies_enabled_group_and_ancestor_replacement"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_windows_system_curl_trust_rejects_user_module_shadowing"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_windows_system_curl_trust_rejects_null_dacl"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_unix_system_curl_trust_rejects_writable_ancestor"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_collecting_ledger_blocker_is_already_monotonic"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_range_diff_check_catches_whitespace_already_committed_in_feature"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_blocker_waits_for_all_surfaces_then_emits_one_fix_batch"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_tuple_drift_never_carries_approval"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_agent_record_cannot_escalate_human_authority"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_finalize_requires_same_tuple_ready_before_content_review"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_github_capture_is_exact_head_terminal_and_fail_closed"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_content_reviewers_must_be_distinct_even_with_different_sessions"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_all_same_surface_blocking_findings_survive_consolidation"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_configured_quorum_cannot_be_replaced_by_one_surface_ledger"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_rehashed_ready_state_with_blockers_is_incoherent_and_finalize_blocks"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_generic_records_cannot_impersonate_hosted_surfaces"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_provider_repository_and_annotation_shape_are_fail_closed"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_hosted_verdict_cannot_be_rehashed_against_failing_provider_evidence"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_nonterminal_or_fabricated_provider_state_cannot_finalize"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_monotonic_chain_detects_deleted_same_tuple_blocker_history"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_mandatory_policy_cannot_be_disabled_when_tooling_baseline_is_enforced"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_diff_identity_is_independent_of_git_color_configuration"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_final_integration_tree_or_diff_drift_is_blocking"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_manifest_strings_cannot_promote_redistribution_without_receipts"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_full_validator_can_reach_ready_after_all_integrity_evidence_is_verified"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_archive_validation_uses_one_authenticated_immutable_snapshot"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_tracked_archive_rejects_reparse_paths_and_nonregular_index_modes"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_ready_promotion_requires_verifier_bytes_from_canonical_target"},
            {"path": "tools/repo_hygiene/test_vendored_native_payloads.py", "test": "test_tracked_archive_rejects_path_identity_swap_during_open"},
            {"path": "tools/repo_hygiene/test_candidate_acceptance.py", "test": "test_live_github_master_head_is_provider_derived_and_exact"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_repo_sweep_reuses_one_plan_and_one_dirty_probe_per_worktree"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_repo_sweep_investigations_resume_in_bounded_content_addressed_pages"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_repo_sweep_investigation_cache_rejects_dirty_source_item_drift"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_repo_sweep_cached_recommendation_cannot_be_rehashed_into_prune_authority"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_remote_feature_delete_fetches_when_tracking_ref_is_missing"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_remote_feature_delete_ignores_narrow_fetch_refspec_for_provider_truth"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_remote_feature_delete_lease_rejects_concurrent_advance"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_remote_feature_delete_blocks_ref_recreated_after_delete"},
            {"path": "tools/repo_hygiene/test_brokered_closeout.py", "test": "test_remote_feature_delete_blocks_when_postdelete_exact_query_fails"},
        ],
        "requiredSymbols": [
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def bootstrap_response_broker_manifest"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def checkpoint_owned_dirty_action_id"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def verify_detached_preservation_commit"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repair_target_push_failure"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def verify_closeout_tooling_current"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repair_missing_evidence"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def auto_branch_from_protected_target"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validate_work_block_start_head_integrated"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validate_content_review_approval_for_finalize"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def target_candidate_acceptance_policy"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def _activation_state"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def candidate_acceptance_enforced"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repository_has_canonical_acceptance_remote"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def candidate_identity"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def evaluate"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def latest_summary"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def validate_for_finalize"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def provider_surface_record"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def verify_live_provider"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def _trusted_system_curl_identity"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def _minimal_system_child_environment"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def _trusted_unix_curl_path_chain"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def system_curl_github_query_command"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def content_review_gate_trust_error"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def _load_chain"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def final_integration_mismatches"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "PROMOTION_VERIFIER_SURFACES"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "PROMOTION_PROVIDER_AUTHORITY_STATE = \"NOT_INSTALLED\""},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _load_promotion_claims"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _validate_promotion_verifier_unchanged"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _manifest_promotion_binding"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _validate_promotion_receipt"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _validate_registered_promotion_claim"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _verify_provider_owner_claim"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _promotion_approval_body"},
            {"path": "tools/repo_hygiene/vendored_native_payloads.py", "contains": "def _archive_bytes_snapshot"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def live_github_issue_comment"},
            {"path": "tools/repo_hygiene/candidate_acceptance.py", "contains": "def live_github_branch_head"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def integration_range_evidence"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repo_sweep_plan"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repo_sweep_tuple"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def reusable_candidate_report"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def investigation_jobs"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def fresh_investigation_report"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def delete_remote_feature_ref"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remote_provider_ref_snapshot"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def preserve_owned_dirty_split"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def apply_detached_dirty_preserve"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def cleanup_foreign_dirty_integrated_branch"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def run_bounded_closeout_process"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def closeout_process_resource_policy"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def apply_windows_process_tree_affinity"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def process_tree_cpu_sample"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def powershell_executable_for_policy"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def closeout_script_command"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def effective_closeout_script_command"},
            {"path": "tools/agent-bridge/powershell_runtime.py", "contains": "def powershell_cim_command"},
            {"path": "tools/agent-bridge/bootstrap_session.py", "contains": "powershell_cim_command"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validation_command_timeout_ms"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validation_full_suite_requested"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validation_command_applies"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def kill_process_tree"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def normalize_closeout_child_status"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def bounded_runner_exit_code"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repo_state_snapshot"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "closeout-history-index.v1"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "rollback-readiness.v1"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def repo_state_snapshot_evidence_hash"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def manifest_path_claims_are_stale"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def recover_dirty_protected_target_to_work_block"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def recovery_command_forbidden_action"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def require_integrity_checked_audit"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def validate_rollback_manifest"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "validate-rollback-manifest"},
            {"path": "tools/closeout/validate-rollback-manifest.ps1", "contains": "validate-rollback-manifest"},
            {"path": "tools/closeout/validate-rollback-manifest.ps1", "contains": "exit $LASTEXITCODE"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "DASHBOARD_ACTIONS_SCHEMA"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "DASHBOARD_ACTION_PREVIEW_SCHEMA"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "DASHBOARD_ACTION_REQUEST_SCHEMA"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "DASHBOARD_ACTION_REQUEST_HISTORY_SCHEMA"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "def dashboard_actions_payload"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "def dashboard_action_preview_payload"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "def dashboard_action_request_payload"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "def dashboard_action_request_history_payload"},
            {"path": "tools/repo_hygiene/closeout_dashboard.py", "contains": "def history_snapshot_payload"},
            {"path": "docs/19-closeout-dashboard-spec.md", "contains": "workflow-comparison"},
            {"path": "docs/19-closeout-dashboard-spec.md", "contains": "round-delta note"},
            {"path": "docs/19-closeout-dashboard-spec.md", "contains": "read-first"},
            {"path": "docs/19-closeout-dashboard-spec.md", "contains": "same work block"},
            {"path": "docs/19-closeout-dashboard-spec.md", "contains": "freshness"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def closeout_command_has_semantic_success_authority"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def bounded_closeout_cli_main"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remote_feature_rows"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def finalize_retry_decision"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remediate_retained_candidates"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def dispatch_agent_conflict_remediation"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def agent_remediation_queue_consumer_plan"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def collect_agent_remediation_results"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def write_evidence_preserving_prune_recovery"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def verify_prune_recovery_artifact"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def write_dirty_worktree_recovery_evidence"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def agent_conflict_resolution_packet"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def verify_repo_closed_postcondition"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def worktree_inspection_state"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def closeout_clean_truth_from_postcondition"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def stop_runtime_services_before_promotion"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def restart_runtime_services_after_clean_promotion"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remediation_freeze_status"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remediation_freeze_staleness"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def assert_remediation_freeze_not_active"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def remediation_packet_template"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def dirty_state_remediation_triage"},
            {"path": "tools/repo_hygiene/brokered_closeout.py", "contains": "def write_review_surface_unavailable_report"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "remediation-freeze-status"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "repo-state"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "--latest-only"},
            {"path": "tools/closeout/write-repo-state.ps1", "contains": "LatestOnly"},
            {"path": "tools/closeout/start-closeout-dashboard.ps1", "contains": "serverProcessId"},
            {"path": "tools/closeout/start-closeout-dashboard.ps1", "contains": "reuse"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "hook-guard"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "--mark-surface-unavailable"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "--require-repo-closed"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "agent-queue"},
            {"path": "tools/repo_hygiene/work_block_cli.py", "contains": "agent-results"},
            {"path": "tools/closeout/Invoke-CloseoutCli.ps1", "contains": "bounded_closeout_cli_main"},
            {"path": "tools/closeout/Invoke-CloseoutCli.ps1", "contains": "closeout-heartbeat"},
            {"path": "tools/closeout/Invoke-CloseoutCli.ps1", "contains": "CLOSEOUT_ADAPTER_HEARTBEAT_SECONDS"},
            {"path": "tools/closeout/work-block-complete.ps1", "contains": "RequireRepoClosed"},
            {"path": "tools/closeout/agent-remediation-queue.ps1", "contains": "agent-queue"},
            {"path": "tools/closeout/agent-remediation-queue.ps1", "contains": "CollectResults"},
            {"path": "tools/closeout/review-quorum.ps1", "contains": "MarkSurfaceUnavailable"},
            {"path": "tools/closeout/remediate-retained-closeout.ps1", "contains": "remediate-retained"},
            {"path": "AGENTS.md", "contains": "Closeout actors must be bounded at the process boundary"},
            {"path": "AGENTS.md", "contains": "semantic success authority"},
            {"path": "AGENTS.md", "contains": "boundedRunnerExitCodes"},
            {"path": "AGENTS.md", "contains": "Hard-clean final responses are blocked unless the repo-closed postcondition passes"},
            {"path": "AGENTS.md", "contains": "closeoutCleanTruth"},
            {"path": "AGENTS.md", "contains": "Closeout Remediation Freeze"},
            {"path": "AGENTS.md", "contains": "agentRemediationQueue.queueRoots"},
            {"path": "AGENTS.md", "contains": "protected-target-noop-closeout"},
            {"path": "AGENTS.md", "contains": "protected-target-dirty-recovery"},
            {"path": "AGENTS.md", "contains": "workBlockBootstrap.autoBranchFromProtectedTarget"},
            {"path": "AGENTS.md", "contains": "workBlockBootstrap.requireIntegratedStartHeadForFinalize"},
            {"path": "AGENTS.md", "contains": "Evidence-preserving transaction prune"},
            {"path": "AGENTS.md", "contains": "PowerShell 7+"},
            {"path": "AGENTS.md", "contains": "repoStateLedger"},
            {"path": "AGENTS.md", "contains": "worktree-inspection.v1"},
            {"path": "AGENTS.md", "contains": "rollbackPolicy"},
            {"path": "CLAUDE.md", "contains": "Closeout actors must be bounded at the process boundary"},
            {"path": "CLAUDE.md", "contains": "semantic success authority"},
            {"path": "CLAUDE.md", "contains": "boundedRunnerExitCodes"},
            {"path": "CLAUDE.md", "contains": "Hard-clean final responses are blocked unless the repo-closed postcondition passes"},
            {"path": "CLAUDE.md", "contains": "closeoutCleanTruth"},
            {"path": "CLAUDE.md", "contains": "Closeout Remediation Freeze"},
            {"path": "CLAUDE.md", "contains": "agentRemediationQueue.queueRoots"},
            {"path": "CLAUDE.md", "contains": "protected-target-noop-closeout"},
            {"path": "CLAUDE.md", "contains": "protected-target-dirty-recovery"},
            {"path": "CLAUDE.md", "contains": "workBlockBootstrap.autoBranchFromProtectedTarget"},
            {"path": "CLAUDE.md", "contains": "workBlockBootstrap.requireIntegratedStartHeadForFinalize"},
            {"path": "CLAUDE.md", "contains": "Evidence-preserving transaction prune"},
            {"path": "CLAUDE.md", "contains": "PowerShell 7+"},
            {"path": "CLAUDE.md", "contains": "repoStateLedger"},
            {"path": "CLAUDE.md", "contains": "worktree-inspection.v1"},
            {"path": "CLAUDE.md", "contains": "rollbackPolicy"},
            {"path": "closeout.config.json", "contains": "closeoutAddendumPersistence"},
            {"path": "closeout.config.json", "contains": "workBlockBootstrap"},
            {"path": "closeout.config.json", "contains": "requireIntegratedStartHeadForFinalize"},
            {"path": "closeout.config.json", "contains": "contentReviewGate"},
            {"path": "closeout.config.json", "contains": "finalizeLoop"},
            {"path": "closeout.config.json", "contains": "remediationFreeze"},
            {"path": "closeout.config.json", "contains": "hardClean"},
            {"path": "closeout.config.json", "contains": "runtimeServices"},
            {"path": "closeout.config.json", "contains": "maxProcessOutputBytes"},
            {"path": "closeout.config.json", "contains": "adapterHeartbeatSeconds"},
            {"path": "closeout.config.json", "contains": "boundedRunnerExitCodes"},
            {"path": "closeout.config.json", "contains": "preferredExecutable"},
            {"path": "closeout.config.json", "contains": "unifiedTruthReport"},
            {"path": "closeout.config.json", "contains": "failureTextPatterns"},
            {"path": "closeout.config.json", "contains": "repoStateLedger"},
            {"path": "closeout.config.json", "contains": "webDashboardSpec"},
            {"path": "closeout.config.json", "contains": "rollbackPolicy"},
            {"path": "tools/agent-bridge/codex_pre_response.ps1", "contains": "bootstrap-response"},
            {"path": "tools/agent-bridge/codex_pre_response.ps1", "contains": "remediation-freeze-status"},
            {"path": "tools/agent-bridge/codex_pre_response.ps1", "contains": "-SkipSessionWorktree"},
            {"path": "tools/agent-bridge/codex_pre_final.ps1", "contains": "remediation-freeze-status"},
            {"path": "tools/agent-bridge/codex_pre_final.ps1", "contains": "-SkipSessionWorktree"},
            {"path": "tools/agent-bridge/codex_bridge_reminder.ps1", "contains": "session_worktree_bootstrap=skipped"},
        ],
    },
    "evidenceRepair": {
        "enabled": True,
        "evidenceRoot": ".closeout-evidence",
        "requiredArtifacts": ["metrics.json", "handoff.json", "session.json", "closeout.json"],
        "requiredFor": ["publish_missing_upstream", "publish_ahead_only", "final_push"],
        "commitMessage": "chore(closeout): repair closeout evidence artifacts",
    },
    "commitMessages": {
        "humanSummaryField": "commitSubject",
        "appendGeneratedWorkBlockDetails": True,
        "rejectGenericSubjects": True,
        "genericSubjectExamples": [
            "brokered closeout checkpoint",
            "brokered closeout evidence repair",
            "merge(closeout): integrate <workBlockId> closeout hardening into <target>",
        ],
    },
    "reviewQuorum": {
        "requiredApprovals": 3,
        "requiredScore": 10,
        "requiredSelfApprovals": 1,
        "requiredIndependentApprovals": 2,
        "selfReviewers": ["codex-self"],
        "independentReviewers": ["ancestry-safety-reviewer", "mutation-scope-reviewer"],
        "allowedReviewers": ["codex", "claude", "human", "local-test", "codex-self", "ancestry-safety-reviewer", "mutation-scope-reviewer"],
        "unavailableStatus": "review_surface_unavailable",
        "insufficientReviewStatus": "insufficient_review_quorum",
        "declaredSurfaces": [
            {
                "surface": "codex-primary-agent",
                "reviewers": ["codex", "codex-self"],
                "recoveryCommand": "Run tools\\closeout\\review-quorum.ps1 with an allowed Codex reviewer or record a review-surface-unavailable report.",
            },
            {
                "surface": "claude-primary-agent",
                "reviewers": ["claude"],
                "recoveryCommand": "Route the packet to Claude review or record a review-surface-unavailable report.",
            },
            {
                "surface": "human-reviewer",
                "reviewers": ["human"],
                "recoveryCommand": "Route the packet to a human reviewer or record a review-surface-unavailable report.",
            },
            {
                "surface": "deterministic-reviewer",
                "reviewers": ["local-test", "ancestry-safety-reviewer", "mutation-scope-reviewer"],
                "recoveryCommand": "Run deterministic policy review or record a review-surface-unavailable report.",
            },
        ],
        "highImpactActions": HIGH_IMPACT_ACTIONS,
        "tupleFields": ["candidateId", "actionId", "evidenceHash", "policyHash", "pinnedRefs"],
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
            "redundant_branch_prune",
            "explicit_protected_worktree_cleanup",
            "agent_conflict_remediation",
            "dirty_cluster_preservation",
            "stale_claim_remediation",
            "remediation_freeze_removal",
        ],
        "manualOnlyActionClasses": [
            "protected_branch",
            "dirty_worktree",
            "locked_worktree",
            "ambiguous_merge_required",
            "active_locked_worktree",
            "unowned_dirty_triage",
        ],
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
        "maxNewInvestigationsPerRun": 2,
        "cleanIntegrateRemoteFeatureBranches": True,
        "deleteRemoteFeatureAfterCleanIntegrate": True,
        "auditedBulkOverride": {
            "enabled": False,
        },
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
    "candidateAcceptance": {
        "enabled": True,
        "schema": "candidate-acceptance.v1",
        "stateRoot": ".claude-state/closeout/acceptance",
        "providerRepository": "layibabalola/MLV-App",
        "requiredSurfaces": [
            "content-self",
            "content-stranger-1",
            "content-stranger-2",
            "hosted-tests",
            "hosted-codeql",
        ],
        "batchUntilAllSurfacesTerminal": True,
        "carryApprovalsAcrossCandidateTuples": False,
        "agentApprovalsGrantHumanAuthority": False,
        "requireReadyForFinalize": True,
    },
    "scripts": REQUIRED_SCRIPT_NAMES,
}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, sort_keys=True))
        handle.write("\n")


def stable_hash(data: Any, length: int = 32) -> str:
    return sha256_text(canonical_json(data), length)


def parse_utc(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_closeout_config(repo_root_arg: Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    override = read_json(repo_root / CONFIG_PATH, {})
    if not isinstance(override, dict):
        raise HygieneError("closeout.config.json must contain a JSON object")
    config = deep_merge(DEFAULT_CLOSEOUT_CONFIG, override)
    state_root_value = normalize_rel(str(config.get("stateRoot") or ""))
    if not state_root_value.startswith(".claude-state/"):
        raise HygieneError("closeout stateRoot must stay under .claude-state/")
    config["policyHash"] = stable_hash(config, 32)
    return config


def config_path_value(config: Dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def closeout_state_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return (repo_root / str(config.get("stateRoot", ".claude-state/closeout"))).resolve()


def work_blocks_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "work-blocks"


def work_block_dir(repo_root: Path, config: Dict[str, Any], work_block_id: str) -> Path:
    safe = str(work_block_id).strip()
    if not safe or any(ch in safe for ch in "\\/"):
        raise HygieneError("invalid workBlockId")
    if ".." in safe:
        raise HygieneError("invalid workBlockId")
    return work_blocks_root(repo_root, config) / safe


def audit_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "audits"


def reviews_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "reviews"


def locks_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "locks"


def path_matches_any(path: str, patterns: Iterable[str]) -> bool:
    value = normalize_rel(path)
    return any(fnmatch.fnmatch(value, normalize_rel(pattern)) for pattern in patterns)


def generated_only_detached_dirty_proof(config: Dict[str, Any], entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    paths = sorted({str(entry.get("path") or "") for entry in entries if str(entry.get("path") or "")})
    patterns = [str(pattern) for pattern in config.get("repoSweep", {}).get("detachedDirtyGeneratedOnlyPatterns", [])]
    matched: List[Dict[str, Any]] = []
    unmatched: List[str] = []
    for path in paths:
        pattern = next((candidate for candidate in patterns if path_matches_any(path, [candidate])), None)
        if pattern:
            matched.append({"path": path, "pattern": pattern})
        else:
            unmatched.append(path)
    return {
        "eligible": bool(paths) and not unmatched,
        "pathCount": len(paths),
        "patterns": patterns,
        "matched": matched,
        "unmatched": unmatched,
    }


def clean_at_start_sensitive_auto_claim_allowed(
    repo_root: Path,
    config: Dict[str, Any],
    path: str,
    entry: Optional[Dict[str, Any]] = None,
) -> bool:
    patterns = config.get("dirty", {}).get("cleanAtStartSensitiveAutoClaim", [])
    if not path_matches_any(path, patterns):
        return False
    status = str((entry or {}).get("status") or "")
    if status.strip() == "??":
        return False
    if any(marker in status for marker in ("A", "D", "R", "C")):
        return False
    if (entry or {}).get("originalPath"):
        return False
    return git_path_tracked(repo_root, path)


def current_branch(repo_root: Path) -> Optional[str]:
    result = run_git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def rev_parse(repo_root: Path, rev: str, *, required: bool = True) -> Optional[str]:
    result = run_git(repo_root, ["rev-parse", "--verify", rev])
    if result.returncode != 0:
        if required:
            raise HygieneError("git ref is missing: %s" % rev)
        return None
    return result.stdout.strip()


def git_stdout(repo_root: Path, args: Sequence[str], *, required: bool = True) -> str:
    result = run_git(repo_root, args)
    if result.returncode != 0:
        if required:
            raise HygieneError("git command failed: git %s\n%s" % (" ".join(args), result.stderr.strip()))
        return ""
    return result.stdout.strip()


def canonical_worktree_root(repo_root: Path) -> Optional[Path]:
    """The MAIN worktree's root -- the same directory for every linked worktree.

    GATE-ID-4. The content-review ledger used to be resolved against `repo_root`, which is
    `git rev-parse --show-toplevel` and therefore PER-WORKTREE. Because `.claude-state/` is
    gitignored, a work-block worktree is a checkout of tracked files only and never contains
    the ledger at all, so the gate resolved a path that did not exist and blocked
    `content_approval_file_missing` -- or, worse, was satisfied by a per-worktree COPY, which
    means a verdict could be written somewhere the next block would never read. Four live
    instances are on record.

    `--git-common-dir` is the shared directory: in the main worktree it is `<main>/.git`, and
    in a LINKED worktree it is still `<main>/.git` (only `--git-dir` is per-worktree). Its
    parent is therefore the one directory every worktree agrees on, which makes the ledger
    ONE FILE BY CONSTRUCTION rather than by everyone remembering to copy it.

    `--path-format=absolute` IS MANDATORY and is not a stylistic preference. Measured on
    git 2.40:

        cwd                       bare --git-common-dir      --path-format=absolute
        canonical root            .git                       C:/.../MLV-App/.git
        canonical subdir (src/)   ../.git                     C:/.../MLV-App/.git
        linked worktree           C:/.../MLV-App/.git         C:/.../MLV-App/.git

    The bare form is CWD-RELATIVE, so its meaning depends on where the caller stands -- which
    is the same cwd-dependence this function exists to remove. Resolving `../.git` against the
    wrong base silently yields a wrong ledger, so the flag is load-bearing.

    TWO FAILURE CASES, AND THEY ARE NOT THE SAME:

    * `repo_root` is not a Git repository at all. Then no linked worktree can exist, so the
      defect this function removes is impossible there, and `repo_root` IS the canonical
      root. Returning it is not a silent fallback to the old behaviour -- it is the same
      answer by a different route. This keeps the validator usable on a plain directory,
      which is how the ledger-parsing tests exercise it without standing up a repo.
    * It IS a repository but the common dir could not be resolved absolutely -- an old Git
      without `--path-format`, or any relative answer. Then the correct root is unknown, and
      guessing `repo_root` would restore the per-worktree behaviour while appearing fixed.
      Returns None, and the caller FAILS CLOSED.
    """
    raw = git_stdout(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"], required=False)
    if raw:
        common = Path(raw.strip())
        if common.is_absolute():
            return common.parent
        # A relative answer means the flag was not honoured by this Git build. Resolving it
        # here would reintroduce exactly the cwd-dependence this exists to remove.
        return None
    # The first call failed. Distinguish "not a repository" from "repository, but the
    # resolution failed" -- only the second is dangerous.
    if not git_stdout(repo_root, ["rev-parse", "--git-dir"], required=False):
        return repo_root
    return None


def run_git_longpaths(repo_root: Path, args: Sequence[str], *, check: bool = False):
    return run_git(repo_root, ["-c", "core.longpaths=true", *args], check=check)


def remote_exists(repo_root: Path, remote: str) -> bool:
    return run_git(repo_root, ["remote", "get-url", remote]).returncode == 0


def is_protected_branch(config: Dict[str, Any], branch: Optional[str]) -> bool:
    if not branch:
        return False
    return path_matches_any(branch, config.get("git", {}).get("protectedBranches", []))


def branch_allowed_by_policy(config: Dict[str, Any], branch: str) -> bool:
    return path_matches_any(branch, config.get("git", {}).get("featureBranchPatterns", []))


def safe_branch_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(value).strip())
    token = re.sub(r"/+", "/", token).strip("./-")
    return token or ("wb-%s" % uuid.uuid4().hex[:8])


def work_block_bootstrap_branch(config: Dict[str, Any], work_block_id: str) -> str:
    bootstrap = config.get("workBlockBootstrap", {})
    prefix = safe_branch_token(str(bootstrap.get("branchPrefix") or "codex/work-block")).strip("/")
    block_token = safe_branch_token(work_block_id).strip("/")
    if block_token.startswith(prefix + "/") or block_token == prefix:
        branch = block_token
    else:
        branch = f"{prefix}/{block_token}"
    if not branch_allowed_by_policy(config, branch):
        raise HygieneError("work block bootstrap branch is not allowed by featureBranchPatterns: %s" % branch)
    return branch


def auto_branch_from_protected_target(repo_root: Path, config: Dict[str, Any], protected_branch: str, work_block_id: str) -> Dict[str, Any]:
    bootstrap = config.get("workBlockBootstrap", {})
    if not bool(bootstrap.get("autoBranchFromProtectedTarget", False)):
        raise HygieneError("work block cannot start on protected branch %s" % protected_branch)
    dirty = dirty_baseline_snapshot(repo_root)
    if dirty["paths"]:
        preview = ", ".join(dirty["paths"][:8])
        if len(dirty["paths"]) > 8:
            preview += ", ..."
        raise HygieneError(
            "work block cannot auto-branch from protected branch %s with dirty paths: %s"
            % (protected_branch, preview)
        )
    branch = work_block_bootstrap_branch(config, work_block_id)
    if rev_parse(repo_root, f"refs/heads/{branch}", required=False):
        raise HygieneError("work block bootstrap branch already exists: %s" % branch)
    start_head = rev_parse(repo_root, "HEAD")
    result = run_git(repo_root, ["checkout", "-b", branch])
    if result.returncode != 0:
        raise HygieneError("failed to create work block bootstrap branch %s: %s" % (branch, result.stderr.strip()))
    return {
        "enabled": True,
        "fromProtectedBranch": protected_branch,
        "createdBranch": branch,
        "startHead": start_head,
        "reason": "protected_branch_auto_branch",
    }


def resolve_work_block_start_head(
    repo_root: Path,
    config: Dict[str, Any],
    requested_start_head: Optional[str],
    current_head: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not requested_start_head:
        return current_head, None
    start_head = rev_parse(repo_root, requested_start_head)
    if not is_ancestor(repo_root, start_head, current_head):
        raise HygieneError(
            "work block startHead override must be an ancestor of current HEAD: %s" % start_head
        )
    target = target_ref_for(repo_root, config)
    target_head = str(target.get("head") or "")
    if not target_head:
        raise HygieneError("work block startHead override requires a resolved target head")
    if not is_ancestor(repo_root, start_head, target_head):
        raise HygieneError(
            "work block startHead override must be contained in target %s: %s"
            % (target.get("ref") or target.get("targetBranch") or "target", start_head)
        )
    return start_head, {
        "requested": requested_start_head,
        "resolved": start_head,
        "currentHead": current_head,
        "targetRef": target.get("ref"),
        "targetHead": target_head,
    }


def target_ref_for(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    git_config = config.get("git", {})
    target_branch = str(git_config.get("targetBranch", "master"))
    remote = str(git_config.get("remote", "origin"))
    has_remote = remote_exists(repo_root, remote)
    if has_remote and bool(git_config.get("fetchBeforeEvidence", True)):
        run_git(repo_root, ["fetch", "--prune", remote, target_branch])
    remote_ref = f"refs/remotes/{remote}/{target_branch}"
    local_ref = f"refs/heads/{target_branch}"
    remote_head = rev_parse(repo_root, remote_ref, required=False) if has_remote else None
    local_head = rev_parse(repo_root, local_ref, required=False)
    if remote_head:
        return {
            "targetBranch": target_branch,
            "remote": remote,
            "mode": "remote",
            "ref": remote_ref,
            "head": remote_head,
            "localHead": local_head,
        }
    if local_head:
        return {
            "targetBranch": target_branch,
            "remote": remote if has_remote else None,
            "mode": "local",
            "ref": local_ref,
            "head": local_head,
            "localHead": local_head,
        }
    raise HygieneError("target branch is missing: %s" % target_branch)


def parse_status_paths(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], check=True)
    raw = result.stdout
    if not raw:
        return []
    parts = raw.split("\0")
    entries: List[Dict[str, Any]] = []
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if not item:
            continue
        code = item[:2]
        path = item[3:]
        original = None
        if code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}:
            if i < len(parts):
                original = parts[i]
                i += 1
        path = normalize_rel(path)
        if path:
            entries.append({"status": code, "path": path, "originalPath": normalize_rel(original or "") or None})
    return entries


def dirty_baseline_snapshot(repo_root: Path) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for entry in parse_status_paths(repo_root):
        path = str(entry["path"])
        entries.append(
            {
                "status": entry.get("status"),
                "path": path,
                "originalPath": entry.get("originalPath"),
                "contentSha256": file_content_hash(repo_root / path),
            }
        )
    entries = sorted(entries, key=lambda item: str(item["path"]))
    return {
        "capturedAt": utc_now(),
        "paths": [str(item["path"]) for item in entries],
        "entries": entries,
    }


def manifest_dirty_baseline_paths(manifest: Dict[str, Any]) -> set[str]:
    baseline = manifest.get("dirtyBaseline")
    if not isinstance(baseline, dict):
        return set()
    paths = baseline.get("paths")
    if isinstance(paths, list):
        return {normalize_rel(str(path)) for path in paths if normalize_rel(str(path))}
    entries = baseline.get("entries")
    if isinstance(entries, list):
        return {normalize_rel(str(item.get("path") if isinstance(item, dict) else "")) for item in entries}
    return set()


def manifest_dirty_baseline_entries(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    baseline = manifest.get("dirtyBaseline")
    if not isinstance(baseline, dict):
        return {}
    entries = baseline.get("entries")
    if not isinstance(entries, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = normalize_rel(str(item.get("path") or ""))
        if path:
            result[path] = item
    return result


def dirty_baseline_change_details(
    worktree_root: Path,
    entry: Dict[str, Any],
    baseline_entry: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not baseline_entry:
        return {"dirtyBaselineContentChanged": False}
    path = normalize_rel(str(entry.get("path") or ""))
    current_hash = file_content_hash(worktree_root / path)
    baseline_hash = baseline_entry.get("contentSha256")
    current_status = str(entry.get("status") or "")
    baseline_status = str(baseline_entry.get("status") or "")
    current_original = normalize_rel(str(entry.get("originalPath") or ""))
    baseline_original = normalize_rel(str(baseline_entry.get("originalPath") or ""))
    changed = (
        current_hash != baseline_hash
        or current_status != baseline_status
        or current_original != baseline_original
    )
    return {
        "dirtyBaselineContentChanged": changed,
        "dirtyBaselineContentSha256": baseline_hash,
        "currentContentSha256": current_hash,
        "dirtyBaselineStatus": baseline_status,
        "dirtyBaselineOriginalPath": baseline_original or None,
    }


def manifest_has_dirty_baseline(manifest: Dict[str, Any]) -> bool:
    return isinstance(manifest.get("dirtyBaseline"), dict)


def baseline_dirty_recovery_command(paths: Sequence[str]) -> str:
    joined = " ".join(sorted({normalize_rel(str(path)) for path in paths if normalize_rel(str(path))}))
    return "split or checkpoint pre-existing dirty content for %s, then rerun work-block-complete -Finalize" % joined


def changed_paths_between(repo_root: Path, target_ref: str, feature_head: str) -> List[str]:
    base = git_stdout(repo_root, ["merge-base", target_ref, feature_head])
    result = run_git(repo_root, ["diff", "--name-only", "-z", f"{base}..{feature_head}"], check=True)
    return sorted(set(nul_separated_paths(result.stdout)))


def load_manifest(repo_root: Path, config: Dict[str, Any], work_block_id: str) -> Dict[str, Any]:
    manifest = read_json(work_block_dir(repo_root, config, work_block_id) / "manifest.json", {})
    if not manifest:
        raise HygieneError("work block manifest is missing: %s" % work_block_id)
    return manifest


def manifest_path_claims(manifest: Dict[str, Any]) -> List[str]:
    return sorted({normalize_rel(path) for path in manifest.get("pathClaims", []) if normalize_rel(str(path))})


def manifest_path_claims_are_stale(repo_root: Path, config: Dict[str, Any], manifest: Dict[str, Any]) -> bool:
    branch = str(manifest.get("branch") or "")
    if not branch:
        return True
    branch_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    if not branch_head:
        return True
    if branch == current_branch(repo_root):
        return False
    if str(manifest.get("state") or "") not in {"blocked", "completed"}:
        return False
    try:
        target = target_ref_for(repo_root, config)
    except HygieneError:
        return False
    target_head = str(target.get("head") or "")
    target_branch = str(target.get("targetBranch") or "")
    if branch != target_branch and target_head and is_ancestor(repo_root, branch_head, target_head):
        return True
    return False


def active_path_claims(repo_root: Path, config: Dict[str, Any]) -> Dict[str, str]:
    claims: Dict[str, str] = {}
    root = work_blocks_root(repo_root, config)
    if not root.exists():
        return claims
    manifests: List[Tuple[Dict[str, Any], Path]] = []
    for manifest_file in root.glob("*/manifest.json"):
        manifest = read_json(manifest_file, {})
        if manifest.get("state") not in {"active", "completed", "finalizing", "blocked"}:
            continue
        if manifest_path_claims_are_stale(repo_root, config, manifest):
            continue
        manifests.append((manifest, manifest_file))
    # State rank is intentionally the first key: an active owner outranks a
    # completed/finalizing/blocked historical claimant before recency decides
    # among owners in the same lifecycle state. The manifest directory name
    # is only a final total-order fallback for legacy manifests with no id.
    for manifest, manifest_file in sorted(
        manifests,
        key=lambda item: (work_block_selection_key(item[0]), item[1].parent.name),
    ):
        block_id = str(manifest.get("workBlockId") or manifest_file.parent.name)
        for claim in manifest_path_claims(manifest):
            claims[claim] = block_id
    return claims


def utc_now_precise() -> str:
    """Microsecond-resolution UTC stamp for broker recency comparisons.

    The shared utc_now() helper is second-truncated and is consumed by the
    agent bridge as well, so it is not widened here. Work blocks started
    inside the same second would otherwise tie on updatedAt and fall through
    to the workBlockId tiebreak, which carries no recency information.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def work_block_recency(manifest: Dict[str, Any]) -> datetime:
    """Most precise start/update instant recorded for a work block.

    Manifests written before the precise fields existed fall back to the
    second-truncated stamps, which floor to .000000 and therefore stay
    ordered against newer precise stamps within the same second.
    """
    for field in ("updatedAtPrecise", "updatedAt", "startedAtPrecise", "startedAt"):
        parsed = parse_utc(str(manifest.get(field) or ""))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def work_block_selection_key(manifest: Dict[str, Any]) -> Tuple[int, datetime, str]:
    state_rank = {"active": 4, "completed": 3, "finalizing": 2, "blocked": 1}
    updated = work_block_recency(manifest)
    return (state_rank.get(str(manifest.get("state")), 0), updated, str(manifest.get("workBlockId") or ""))


def work_block_selection_summary(manifest: Dict[str, Any], reason: str, candidate_count: int) -> Dict[str, Any]:
    return {
        "workBlockId": str(manifest.get("workBlockId") or ""),
        "reason": reason,
        "candidateCount": candidate_count,
        "state": manifest.get("state"),
        "updatedAt": manifest.get("updatedAt"),
    }


def attach_work_block_selection(manifest: Dict[str, Any], reason: str, candidate_count: int) -> Dict[str, Any]:
    selected = dict(manifest)
    selected["workBlockSelection"] = work_block_selection_summary(selected, reason, candidate_count)
    return selected


def append_event(repo_root: Path, config: Dict[str, Any], work_block_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    block_dir = work_block_dir(repo_root, config, work_block_id)
    events_path = block_dir / "events.jsonl"
    previous_hash: Optional[str] = None
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                previous_hash = str(row.get("eventHash") or stable_hash(row))
    row = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "eventId": str(uuid.uuid4()),
        "createdAt": utc_now(),
        "workBlockId": work_block_id,
        "previousEventHash": previous_hash,
        **event,
    }
    clean = dict(row)
    row["eventHash"] = stable_hash(clean)
    append_jsonl(events_path, row)
    append_jsonl(closeout_state_root(repo_root, config) / "ledger" / "events.jsonl", row)
    return row


@contextlib.contextmanager
def broker_lease(repo_root: Path, config: Dict[str, Any], name: str, lease_seconds: Optional[int] = None) -> Iterator[Path]:
    root = locks_root(repo_root, config)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ("%s.lock" % name)
    ttl = int(lease_seconds or 300)
    now_dt = datetime.now(timezone.utc)
    payload = {"pid": os.getpid(), "createdAt": utc_now(), "leaseSeconds": ttl}
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            existing = read_json(lock_path, {})
            created = parse_utc(str(existing.get("createdAt") or ""))
            existing_ttl = int(existing.get("leaseSeconds") or ttl)
            if created and created + timedelta(seconds=existing_ttl) <= now_dt:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
            raise HygieneError("closeout lease is already held: %s" % lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def write_audit(
    repo_root: Path,
    config: Dict[str, Any],
    audit_type: str,
    payload: Dict[str, Any],
    *,
    work_block_id: Optional[str] = None,
    outcome: str = "recorded",
) -> Dict[str, Any]:
    row = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "auditType": audit_type,
        "outcome": outcome,
        "createdAt": utc_now(),
        "workBlockId": work_block_id,
        "policyHash": config.get("policyHash"),
        "payload": payload,
    }
    row["auditHash"] = stable_hash(row)
    name = "%s-%s-%s.json" % (
        row["createdAt"].replace(":", "").replace("+", "Z"),
        audit_type,
        row["auditHash"][:12],
    )
    write_json(audit_root(repo_root, config) / name, row)
    append_jsonl(audit_root(repo_root, config) / "audits.jsonl", row)
    return row


def remediation_freeze_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("remediationFreeze", {})
    return value if isinstance(value, dict) else {}


def truthy_env_value(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def remediation_freeze_marker_path(repo_root: Path, config: Dict[str, Any]) -> Path:
    marker = normalize_rel(str(remediation_freeze_policy(config).get("markerPath") or ".claude-state/closeout-remediation.freeze"))
    return safe_repo_path(repo_root, marker)


def remediation_freeze_audit_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    root = normalize_rel(str(remediation_freeze_policy(config).get("generatedAuditRoot") or ".claude-state/closeout-log/remediation-freeze"))
    return safe_repo_path(repo_root, root)


def remediation_freeze_audit_rel_path(repo_root: Path, config: Dict[str, Any], audit_hash: str) -> str:
    root = remediation_freeze_audit_root(repo_root, config)
    return normalize_rel(str((root / f"sha256-{audit_hash}.json").resolve().relative_to(repo_root.resolve())))


def remediation_freeze_marker_payload(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    marker = remediation_freeze_marker_path(repo_root, config)
    if not marker.exists():
        return {}
    try:
        payload = read_json(marker, {})
    except Exception as exc:
        return {"_readError": str(exc)}
    return payload if isinstance(payload, dict) else {"_readError": "marker payload is not an object"}


def remediation_freeze_packet_payload(
    repo_root: Path,
    marker_payload: Dict[str, Any],
) -> Dict[str, Any]:
    packet_path = normalize_rel(str(marker_payload.get("remediationPacketPath") or ""))
    if not packet_path:
        return {"packet": None, "packetPath": None, "readError": "marker has no remediationPacketPath"}
    try:
        resolved = safe_repo_path(repo_root, packet_path)
    except Exception as exc:
        return {"packet": None, "packetPath": packet_path, "readError": str(exc)}
    if not resolved.exists():
        return {"packet": None, "packetPath": packet_path, "readError": "remediation packet is missing"}
    try:
        packet = read_json(resolved, {})
    except Exception as exc:
        return {"packet": None, "packetPath": packet_path, "readError": str(exc)}
    if not isinstance(packet, dict):
        return {"packet": None, "packetPath": packet_path, "readError": "remediation packet is not an object"}
    return {"packet": packet, "packetPath": packet_path, "readError": None}


def remediation_freeze_dirty_signature(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    signature: List[Dict[str, Any]] = []
    for row in rows:
        signature.append(
            {
                "path": normalize_rel(str(row.get("path") or "")),
                "status": str(row.get("status") or ""),
                "tracked": bool(row.get("tracked")) if "tracked" in row else None,
                "deleted": bool(row.get("deleted")) if "deleted" in row else None,
                "gitObjectId": row.get("gitObjectId"),
                "byteSha256": row.get("byteSha256"),
            }
        )
    return sorted(signature, key=lambda item: (str(item["path"]), str(item["status"])))


def remediation_freeze_staleness(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    work_block_id: Optional[str] = None,
) -> Dict[str, Any]:
    marker_payload = remediation_freeze_marker_payload(repo_root, config)
    packet_info = remediation_freeze_packet_payload(repo_root, marker_payload)
    packet = packet_info.get("packet") if isinstance(packet_info.get("packet"), dict) else None
    reasons: List[Dict[str, Any]] = []

    if marker_payload.get("_readError"):
        reasons.append({"kind": "marker_unreadable", "error": marker_payload.get("_readError")})
    if packet_info.get("readError"):
        reasons.append({"kind": "packet_unreadable", "error": packet_info.get("readError"), "path": packet_info.get("packetPath")})

    current_branch_name = current_branch(repo_root)
    current_head = rev_parse(repo_root, "HEAD", required=False)
    current_target = target_ref_for(repo_root, config)
    current_dirty = remediation_dirty_path_fingerprints(repo_root, parse_status_paths(repo_root))
    current_dirty_signature = remediation_freeze_dirty_signature(current_dirty)

    packet_branch = packet.get("currentBranch") if packet else None
    packet_detection = packet.get("detection") if packet and isinstance(packet.get("detection"), dict) else {}
    packet_work_block_id = packet_detection.get("workBlockId") if isinstance(packet_detection, dict) else None
    pinned_refs = packet.get("pinnedRefs") if packet and isinstance(packet.get("pinnedRefs"), dict) else {}
    packet_feature = pinned_refs.get("feature") if isinstance(pinned_refs.get("feature"), dict) else {}
    packet_target = pinned_refs.get("target") if isinstance(pinned_refs.get("target"), dict) else {}
    packet_dirty_signature = remediation_freeze_dirty_signature(packet.get("dirtyPaths") or []) if packet else []

    if packet and packet_branch != current_branch_name:
        reasons.append({"kind": "branch_mismatch", "packet": packet_branch, "current": current_branch_name})
    if work_block_id and packet_work_block_id and packet_work_block_id != work_block_id:
        reasons.append({"kind": "work_block_id_mismatch", "packet": packet_work_block_id, "current": work_block_id})
    if packet and packet_feature.get("head") != current_head:
        reasons.append({"kind": "feature_head_mismatch", "packet": packet_feature.get("head"), "current": current_head})
    if packet and packet_target.get("head") != current_target.get("head"):
        reasons.append({"kind": "target_head_mismatch", "packet": packet_target.get("head"), "current": current_target.get("head")})
    if packet and packet_dirty_signature != current_dirty_signature:
        reasons.append(
            {
                "kind": "dirty_state_mismatch",
                "packetPaths": [str(item["path"]) for item in packet_dirty_signature],
                "currentPaths": [str(item["path"]) for item in current_dirty_signature],
            }
        )

    marker_hash = marker_payload.get("remediationPacketHash")
    packet_hash = packet.get("remediationPacketHash") if packet else None
    if marker_hash and packet_hash and marker_hash != packet_hash:
        reasons.append({"kind": "packet_hash_mismatch", "marker": marker_hash, "packet": packet_hash})

    marker_policy = marker_payload.get("policyHash")
    packet_policy = packet.get("policyHash") if packet else None
    current_policy = config.get("policyHash")
    if marker_policy and marker_policy != current_policy:
        reasons.append({"kind": "marker_policy_mismatch", "marker": marker_policy, "current": current_policy})
    if packet_policy and packet_policy != current_policy:
        reasons.append({"kind": "packet_policy_mismatch", "packet": packet_policy, "current": current_policy})

    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "markerPayload": marker_payload,
        "packetPath": packet_info.get("packetPath"),
        "packetHash": packet_hash,
        "current": {
            "branch": current_branch_name,
            "head": current_head,
            "target": current_target,
            "dirtyPaths": [str(item["path"]) for item in current_dirty_signature],
        },
    }


def write_remediation_freeze_audit_packet(
    repo_root: Path,
    config: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    packet_type: str = "remediation_freeze",
) -> Dict[str, Any]:
    row = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "packetType": packet_type,
        "createdAt": utc_now(),
        "policyHash": config.get("policyHash"),
        "payload": payload,
    }
    audit_hash = stable_hash(row, 64)
    row["auditHash"] = audit_hash
    target = remediation_freeze_audit_root(repo_root, config) / f"sha256-{audit_hash}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        write_json(target, row)
    return {
        "packetType": packet_type,
        "auditHash": audit_hash,
        "path": normalize_rel(str(target.resolve().relative_to(repo_root.resolve()))),
    }


def remediation_freeze_status(
    repo_root_arg: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    action: str = "status",
    write_audit_packet: bool = False,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = config or load_closeout_config(repo_root)
    policy = remediation_freeze_policy(config)
    enabled = bool(policy.get("enabled", True))
    env_var = str(policy.get("envVar") or "CLOSEOUT_REMEDIATION_FREEZE")
    marker = remediation_freeze_marker_path(repo_root, config)
    marker_exists = marker.exists()
    env_active = truthy_env_value(os.environ.get(env_var))
    status = {
        "status": "frozen" if enabled and (marker_exists or env_active) else "clear",
        "active": bool(enabled and (marker_exists or env_active)),
        "enabled": enabled,
        "action": action,
        "markerPath": normalize_rel(str(marker.resolve().relative_to(repo_root.resolve()))),
        "markerExists": marker_exists,
        "envVar": env_var,
        "envActive": env_active,
        "generatedAuditRoot": normalize_rel(str(remediation_freeze_audit_root(repo_root, config).resolve().relative_to(repo_root.resolve()))),
        "clusterLedgerRoot": normalize_rel(str(policy.get("clusterLedgerRoot") or ".claude-state/closeout-log/remediation-clusters")),
        "blockedLifecycleActions": list(policy.get("blockedLifecycleActions") or []),
    }
    if status["active"] and write_audit_packet:
        status["auditPacket"] = write_remediation_freeze_audit_packet(
            repo_root,
            config,
            {
                "action": action,
                "active": True,
                "markerExists": marker_exists,
                "envActive": env_active,
                "markerPath": status["markerPath"],
            },
        )
    return status


def remove_remediation_freeze(repo_root_arg: Path, *, work_block_id: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="remove-remediation-freeze", write_audit_packet=True)
    if not freeze["active"]:
        result = {"status": "success", "reason": "freeze_not_active", "freeze": freeze}
        write_audit(repo_root, config, "remediation_freeze_removed", result, work_block_id=work_block_id, outcome="success")
        return result
    if freeze["envActive"]:
        result = {"status": "blocked", "reason": "remediation_freeze_env_active", "freeze": freeze}
        write_audit(repo_root, config, "remediation_freeze_removed", result, work_block_id=work_block_id, outcome="blocked")
        return result

    stale_freeze = remediation_freeze_staleness(repo_root, config, work_block_id=work_block_id)
    postcondition = repo_closed_postcondition_state(repo_root, config, work_block_id=work_block_id, write_artifacts=True)
    thawable_blocker_kinds = {"remediation_freeze_active", "stale_transaction_branches"}
    if stale_freeze["stale"]:
        thawable_blocker_kinds.update({"non_exempt_dirty_files", "non_exempt_untracked_files"})
    non_thawable_blockers = [
        item for item in (postcondition.get("blockers") or []) if str(item.get("kind")) not in thawable_blocker_kinds
    ]
    if non_thawable_blockers:
        result = {
            "status": "blocked",
            "reason": "repo_closed_postcondition_failed",
            "freeze": freeze,
            "staleFreezeEvidence": stale_freeze,
            "postcondition": postcondition,
        }
        write_audit(repo_root, config, "remediation_freeze_removed", result, work_block_id=work_block_id, outcome="blocked")
        return result

    target = target_ref_for(repo_root, config)
    candidate_id = "candidate:remediation-freeze-removal:%s" % stable_hash(
        {
            "freeze": freeze,
            "staleFreezeEvidence": stale_freeze,
            "postcondition": postcondition.get("closeoutCleanTruth"),
            "head": rev_parse(repo_root, "HEAD", required=False),
        },
        16,
    )
    action_id = "action:remove-remediation-freeze:%s" % stable_hash(
        {"candidateId": candidate_id, "markerPath": freeze.get("markerPath")},
        16,
    )
    evidence = {
        "freeze": freeze,
        "staleFreezeEvidence": stale_freeze,
        "postcondition": postcondition,
        "candidateId": candidate_id,
        "actionId": action_id,
        "target": target,
    }
    quorum = ensure_autonomous_quorum(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=stable_hash(evidence, 64),
        pinned_refs={
            "target": target,
            "remoteAdvertisedTargetHead": target.get("head") if target.get("mode") == "remote" else None,
            "feature": {"branch": current_branch(repo_root), "head": rev_parse(repo_root, "HEAD", required=False)},
        },
        evidence=evidence,
        action_class="remediation_freeze_removal",
        blockers=[],
    )
    if not quorum["quorum"]["ok"]:
        result = {
            "status": "blocked",
            "reason": quorum["quorum"]["reason"] or "review_quorum_blocked",
            "freeze": freeze,
            "staleFreezeEvidence": stale_freeze,
            "quorum": quorum["quorum"],
            "quorumResult": quorum,
        }
        write_audit(repo_root, config, "remediation_freeze_removed", result, work_block_id=work_block_id, outcome="blocked")
        return result

    marker = remediation_freeze_marker_path(repo_root, config)
    marker.unlink(missing_ok=True)
    cleared = remediation_freeze_status(repo_root, config, action="remove-remediation-freeze-post", write_audit_packet=True)
    result = {
        "status": "success",
        "reason": "remediation_freeze_removed",
        "freeze": freeze,
        "staleFreezeEvidence": stale_freeze,
        "postcondition": postcondition,
        "quorum": quorum["quorum"],
        "quorumResult": quorum,
        "clearedFreeze": cleared,
    }
    write_audit(repo_root, config, "remediation_freeze_removed", result, work_block_id=work_block_id, outcome="success")
    return result


def assert_remediation_freeze_not_active(repo_root: Path, config: Dict[str, Any], *, action: str) -> Dict[str, Any]:
    status = remediation_freeze_status(repo_root, config, action=action, write_audit_packet=True)
    if status["active"]:
        raise HygieneError("remediation freeze active; lifecycle action blocked: %s" % action)
    return status


def guard_closeout_hook(repo_root_arg: Path, *, hook_name: str) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    status = assert_remediation_freeze_not_active(repo_root, config, action=hook_name)
    return {"status": "ok", "hookName": hook_name, "freeze": status}


def remote_advertised_target_head(repo_root: Path, config: Dict[str, Any], target_branch: str) -> Optional[str]:
    remote = str(config.get("git", {}).get("remote", "origin"))
    if not remote or not remote_exists(repo_root, remote):
        return None
    result = run_git(repo_root, ["ls-remote", remote, f"refs/heads/{target_branch}"])
    if result.returncode != 0:
        return None
    first = (result.stdout or "").splitlines()[0:1]
    if not first:
        return None
    return first[0].split()[0] if first[0].split() else None


def git_hash_object_id(repo_root: Path, rel_path: str) -> Optional[str]:
    path = safe_repo_path(repo_root, rel_path)
    if not path.exists() or path.is_dir():
        return None
    result = run_git(repo_root, ["hash-object", "--no-filters", "--", rel_path])
    return result.stdout.strip() if result.returncode == 0 else None


def file_mode_string(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return oct(path.stat().st_mode & 0o777)


def remediation_dirty_path_fingerprints(repo_root: Path, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        rel_path = str(entry.get("path") or "")
        path = safe_repo_path(repo_root, rel_path)
        deleted = "D" in str(entry.get("status") or "") or not path.exists()
        tracked = run_git(repo_root, ["ls-files", "--error-unmatch", "--", rel_path]).returncode == 0
        rows.append(
            {
                "path": rel_path,
                "status": entry.get("status"),
                "tracked": tracked,
                "deleted": deleted,
                "fileMode": None if deleted else file_mode_string(path),
                "gitObjectId": None if deleted else git_hash_object_id(repo_root, rel_path),
                "byteSha256": None if deleted else file_content_hash(path),
            }
        )
    return sorted(rows, key=lambda item: str(item["path"]))


def git_common_dir(repo_root: Path) -> Path:
    raw = git_stdout(repo_root, ["rev-parse", "--git-common-dir"], required=False)
    path = Path(raw) if raw else repo_root / ".git"
    return path if path.is_absolute() else (repo_root / path).resolve()


def primary_worktree_root(repo_root: Path) -> Optional[Path]:
    common_dir = git_common_dir(repo_root)
    if common_dir.name != ".git":
        return None
    return common_dir.parent.resolve()


def remediation_hook_guard_proof(repo_root: Path) -> Dict[str, Any]:
    common_dir = git_common_dir(repo_root)
    rows: List[Dict[str, Any]] = []
    for hook_name in ("pre-commit", "pre-push"):
        hook_path = common_dir / "hooks" / hook_name
        text = hook_path.read_text(encoding="utf-8", errors="replace") if hook_path.exists() and hook_path.is_file() else ""
        rows.append(
            {
                "hookName": hook_name,
                "path": str(hook_path),
                "exists": hook_path.exists(),
                "dispatchesToGuardedRepoCode": "hook-guard" in text and "Invoke-CloseoutCli.ps1" in text,
            }
        )
    return {"gitCommonDir": str(common_dir), "hooks": rows}


def process_quiescence_evidence(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    lock_root = locks_root(repo_root, config)
    locks = sorted(str(path.relative_to(repo_root)) for path in lock_root.glob("*.lock")) if lock_root.exists() else []
    return {
        "checkedAt": utc_now(),
        "currentPid": os.getpid(),
        "activeCloseoutLockFiles": locks,
        "quiescent": not locks,
    }


def remediation_packet_template(
    repo_root_arg: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    reason: str,
    detection: Optional[Dict[str, Any]] = None,
    action_list: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = config or load_closeout_config(repo_root)
    branch = current_branch(repo_root)
    target = target_ref_for(repo_root, config)
    remote_advertised = remote_advertised_target_head(repo_root, config, str(target["targetBranch"]))
    manifests = branch_work_block_candidates(repo_root, config, branch or "")
    manifest_rows = []
    for manifest in manifests:
        manifest_rows.append(
            {
                "workBlockId": manifest.get("workBlockId"),
                "state": manifest.get("state"),
                "branch": manifest.get("branch"),
                "worktree": manifest.get("worktree"),
                "lease": manifest.get("lease"),
                "owners": manifest.get("actor"),
                "pathClaims": manifest_path_claims(manifest),
                "selectionReason": (manifest.get("workBlockSelection") or {}).get("reason"),
                "manifestHash": stable_hash(manifest),
            }
        )
    status_entries = parse_status_paths(repo_root)
    dirty_fingerprints = remediation_dirty_path_fingerprints(repo_root, status_entries)
    generated_patterns = list(config.get("paths", {}).get("generated", []))
    sensitive_patterns = list(config.get("paths", {}).get("sensitive", []))
    actions = list(action_list or [])
    base = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "packetType": "closeout_remediation_packet",
        "reason": reason,
        "selectedWorktreePath": str(repo_root),
        "currentBranch": branch,
        "targetBranch": target["targetBranch"],
        "localTargetHead": target.get("localHead"),
        "remoteTrackingTargetHead": target.get("head") if target.get("mode") == "remote" else None,
        "remoteAdvertisedTargetHead": remote_advertised,
        "workBlocks": manifest_rows,
        "staleClaims": active_path_claims(repo_root, config),
        "dirtyPaths": dirty_fingerprints,
        "generatedPathExclusions": generated_patterns,
        "sensitiveRootClassification": {
            "patterns": sensitive_patterns,
            "dirtySensitivePaths": [
                item["path"]
                for item in dirty_fingerprints
                if path_matches_any(str(item["path"]), sensitive_patterns)
            ],
        },
        "hookGuardProof": remediation_hook_guard_proof(repo_root),
        "processQuiescence": process_quiescence_evidence(repo_root, config),
        "actionList": actions,
        "policyHash": config.get("policyHash"),
        "pinnedRefs": {
            "target": target,
            "remoteAdvertisedTargetHead": remote_advertised,
            "feature": {"branch": branch, "head": rev_parse(repo_root, "HEAD", required=False)},
        },
        "recoveryCommands": [
            "Remove the remediation freeze only after exact-tuple quorum and pre-removal revalidation.",
            "Rerun tools\\closeout\\work-block-complete.ps1 -RepoRoot . -Finalize after preservation ledgers prove all dirty source paths are backed.",
        ],
    }
    if detection is not None:
        base["detection"] = {
            "workBlockId": detection.get("workBlockId"),
            "detectorHash": detection.get("detectorHash"),
            "mixedDirty": detection.get("mixedDirty", []),
            "ownedDirty": detection.get("ownedDirty", []),
            "unownedDirty": detection.get("unownedDirty", []),
            "foreignDirty": detection.get("foreignDirty", []),
        }
    base["remediationPacketHash"] = stable_hash(base, 64)
    base["quorum"] = {
        "tupleFields": ["candidateId", "actionId", "remediationPacketHash", "policyHash", "pinnedRefs"],
        "requiredReviewerScore": remediation_freeze_policy(config).get("requiredReviewerScore", 10),
        "requiredReviewers": remediation_freeze_policy(config).get("requiredReviewers", []),
        "blockersAllowed": False,
    }
    return base


def write_remediation_packet(repo_root: Path, config: Dict[str, Any], packet: Dict[str, Any]) -> Dict[str, Any]:
    packet_hash = str(packet.get("remediationPacketHash") or stable_hash(packet, 64))
    target = remediation_freeze_audit_root(repo_root, config) / f"sha256-{packet_hash}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, packet)
    return {"packetHash": packet_hash, "path": normalize_rel(str(target.resolve().relative_to(repo_root.resolve())))}


def enter_remediation_freeze(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    reason: str,
    detection: Optional[Dict[str, Any]] = None,
    action_list: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    packet = remediation_packet_template(repo_root, config, reason=reason, detection=detection, action_list=action_list)
    packet_ref = write_remediation_packet(repo_root, config, packet)
    marker = remediation_freeze_marker_path(repo_root, config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "reason": reason,
        "remediationPacketHash": packet_ref["packetHash"],
        "remediationPacketPath": packet_ref["path"],
        "policyHash": config.get("policyHash"),
    }
    write_json(marker, marker_payload)
    return {
        "status": "frozen",
        "reason": reason,
        "markerPath": normalize_rel(str(marker.resolve().relative_to(repo_root.resolve()))),
        "packet": packet_ref,
    }


def git_cherry_patch_equivalent(repo_root: Path, target_ref: str, feature_head: str) -> bool:
    result = run_git(repo_root, ["cherry", target_ref, feature_head])
    if result.returncode != 0:
        return False
    return not any(line.startswith("+") for line in result.stdout.splitlines() if line.strip())


def dirty_state_remediation_triage(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any]) -> Dict[str, Any]:
    dirty_blockers = bool(detection.get("mixedDirty") or detection.get("unownedDirty"))
    target_head = str(detection.get("targetHead") or "")
    feature_head = str(detection.get("featureHead") or "")
    branch = str(detection.get("branch") or "")
    ancestor = bool(feature_head and target_head and is_ancestor(repo_root, feature_head, target_head))
    tree_equivalent = bool(feature_head and target_head and tree_hash(repo_root, feature_head) == tree_hash(repo_root, target_head))
    patch_equivalent = bool(feature_head and target_head and git_cherry_patch_equivalent(repo_root, target_head, feature_head))
    upstream = upstream_for(repo_root, branch) if branch else None
    gone_upstream_after_integration = upstream is None and (ancestor or tree_equivalent or patch_equivalent)
    required = bool(dirty_blockers and (ancestor or tree_equivalent or patch_equivalent or gone_upstream_after_integration))
    return {
        "status": "remediation_required" if required else "not_required",
        "required": required,
        "reason": "dirty_state_remediation_required" if required else None,
        "branch": branch,
        "featureHead": feature_head,
        "targetHead": target_head,
        "ancestorOfTarget": ancestor,
        "treeEquivalentToTarget": tree_equivalent,
        "patchEquivalentToTarget": patch_equivalent,
        "upstream": upstream,
        "goneUpstreamAfterIntegration": gone_upstream_after_integration,
        "dirtyBlockers": {
            "mixedDirty": [item.get("path") for item in detection.get("mixedDirty", [])],
            "ownedDirty": [item.get("path") for item in detection.get("ownedDirty", [])],
            "unownedDirty": [item.get("path") for item in detection.get("unownedDirty", [])],
        },
    }


def positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def closeout_failure_text_patterns(config: Dict[str, Any]) -> List[str]:
    raw = config.get("locking", {}).get("failureTextPatterns", KNOWN_CLOSEOUT_FAILURE_TEXT)
    if not isinstance(raw, list):
        raw = KNOWN_CLOSEOUT_FAILURE_TEXT
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def closeout_command_timeout_ms(config: Dict[str, Any], closeout_args: Sequence[str]) -> int:
    command = str(closeout_args[0]) if closeout_args else ""
    locking = config.get("locking", {})
    repair = config.get("autoEligibilityRepair", {})
    detect_timeout = positive_int(locking.get("detectTimeoutMs"), 120000)
    finalize_timeout = positive_int(locking.get("finalizeTimeoutMs"), 600000)
    repair_timeout = positive_int(repair.get("timeoutMs"), 300000)
    sweep_timeout = positive_int(locking.get("repoSweepTimeoutMs"), finalize_timeout)
    if command == "finalize" or (command == "complete" and "--finalize" in closeout_args):
        return finalize_timeout
    if command in {"repair", "checkpoint", "dirty-split"}:
        return repair_timeout
    if command in {"sweep", "orphan-quarantine", "remediate-retained"}:
        return sweep_timeout
    return detect_timeout


def closeout_max_process_output_bytes(config: Dict[str, Any]) -> int:
    return positive_int(config.get("locking", {}).get("maxProcessOutputBytes"), 1048576)


def bounded_runner_exit_codes(config: Dict[str, Any]) -> Dict[str, int]:
    configured = config.get("locking", {}).get("boundedRunnerExitCodes")
    if not isinstance(configured, dict):
        configured = {}
    return {
        key: positive_int(configured.get(key), default)
        for key, default in BOUNDED_RUNNER_EXIT_CODE_DEFAULTS.items()
    }


def bounded_runner_exit_code(config: Dict[str, Any], status: str) -> int:
    key = BOUNDED_RUNNER_STATUS_EXIT_CODE_KEYS.get(status)
    if key is None:
        return 1
    return bounded_runner_exit_codes(config)[key]


def process_affinity_mask(cores: Any) -> Optional[int]:
    requested = positive_int(cores, 0)
    if requested <= 0:
        return None
    available = max(1, int(os.cpu_count() or 1))
    capped = min(requested, available)
    return (1 << capped) - 1


def _normalize_priority_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _windows_priority_creationflags(priority: Any) -> int:
    if os.name != "nt":
        return 0
    normalized = _normalize_priority_name(priority)
    mapping = {
        "idle": getattr(subprocess, "IDLE_PRIORITY_CLASS", 0),
        "below_normal": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0),
        "normal": getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0),
        "above_normal": getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0),
    }
    return int(mapping.get(normalized, 0) or 0)


def closeout_process_resource_policy(
    config: Dict[str, Any],
    closeout_args: Optional[Sequence[str]] = None,
    command_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    resources = config.get("processResources", {})
    if not isinstance(resources, dict):
        resources = {}
    command_config = command_config or {}
    profile = "validation" if closeout_args and str(closeout_args[0]) == "validation" else "default"
    priority = command_config.get("priority")
    if priority is None and profile == "validation":
        priority = resources.get("validationPriority")
    if priority is None:
        priority = resources.get("defaultPriority", "below_normal")
    affinity_default = positive_int(resources.get("validationAffinityCores"), 0) if profile == "validation" else positive_int(resources.get("defaultAffinityCores"), 0)
    affinity_cores = positive_int(command_config.get("affinityCores"), affinity_default)
    watchdog = resources.get("cpuWatchdog", {})
    if not isinstance(watchdog, dict):
        watchdog = {}
    command_watchdog = command_config.get("cpuWatchdog")
    if isinstance(command_watchdog, dict):
        watchdog = deep_merge(watchdog, command_watchdog)
    threshold = positive_float(watchdog.get("thresholdPercent"), 80.0)
    sustained = positive_float(watchdog.get("sustainedSeconds"), 30.0)
    sample_interval = positive_float(watchdog.get("sampleIntervalSeconds"), 2.0)
    return {
        "profile": profile,
        "priority": _normalize_priority_name(priority) or "below_normal",
        "priorityCreationFlags": _windows_priority_creationflags(priority),
        "affinityCores": affinity_cores,
        "affinityMask": process_affinity_mask(affinity_cores),
        "cpuWatchdog": {
            "enabled": bool(watchdog.get("enabled", False)),
            "thresholdPercent": threshold,
            "sustainedSeconds": sustained,
            "sampleIntervalSeconds": sample_interval,
            "requireNoOutputProgress": bool(watchdog.get("requireNoOutputProgress", True)),
        },
    }


def apply_windows_process_affinity(process: subprocess.Popen, affinity_mask: Optional[int]) -> Dict[str, Any]:
    if os.name != "nt" or not affinity_mask:
        return {"applied": False, "reason": "not_requested" if not affinity_mask else "non_windows"}
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetProcessAffinityMask.restype = ctypes.c_int
        ok = bool(kernel32.SetProcessAffinityMask(int(process._handle), ctypes.c_size_t(int(affinity_mask))))  # type: ignore[attr-defined]
        result: Dict[str, Any] = {"applied": ok, "affinityMask": int(affinity_mask)}
        if not ok:
            result["error"] = ctypes.get_last_error()
        return result
    except Exception as exc:
        return {"applied": False, "affinityMask": int(affinity_mask), "error": str(exc)}


def apply_windows_pid_affinity(pid: int, affinity_mask: Optional[int]) -> Dict[str, Any]:
    if os.name != "nt" or not affinity_mask:
        return {"pid": int(pid), "applied": False, "reason": "not_requested" if not affinity_mask else "non_windows"}
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0200, False, int(pid))
        if not handle:
            return {"pid": int(pid), "applied": False, "affinityMask": int(affinity_mask), "error": ctypes.get_last_error()}
        try:
            ok = bool(kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(int(affinity_mask))))
            result: Dict[str, Any] = {"pid": int(pid), "applied": ok, "affinityMask": int(affinity_mask)}
            if not ok:
                result["error"] = ctypes.get_last_error()
            return result
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        return {"pid": int(pid), "applied": False, "affinityMask": int(affinity_mask), "error": str(exc)}


def apply_windows_process_tree_affinity(root_pid: int, affinity_mask: Optional[int]) -> Dict[str, Any]:
    if os.name != "nt" or not affinity_mask:
        return {"applied": False, "reason": "not_requested" if not affinity_mask else "non_windows", "pids": []}
    pids = _windows_process_tree_pids(int(root_pid))
    results = [apply_windows_pid_affinity(pid, affinity_mask) for pid in pids]
    return {
        "applied": any(bool(item.get("applied")) for item in results),
        "affinityMask": int(affinity_mask),
        "pids": pids,
        "results": results,
    }


def _windows_parent_process_map() -> Dict[int, int]:
    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid:
            return {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            parent_by_pid: Dict[int, int] = {}
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return {}
            while True:
                parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
            return parent_by_pid
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return {}


def _windows_process_tree_pids(root_pid: int) -> List[int]:
    root = int(root_pid)
    parent_by_pid = _windows_parent_process_map()
    if not parent_by_pid:
        return [root]
    tree = {root}
    changed = True
    while changed:
        changed = False
        for pid, parent in parent_by_pid.items():
            if parent in tree and pid not in tree:
                tree.add(pid)
                changed = True
    return sorted(tree)


def _windows_process_cpu_seconds(pid: int) -> Optional[float]:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            handle = kernel32.OpenProcess(0x0400, False, int(pid))
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            kernel_ticks = (int(kernel.dwHighDateTime) << 32) + int(kernel.dwLowDateTime)
            user_ticks = (int(user.dwHighDateTime) << 32) + int(user.dwLowDateTime)
            return (kernel_ticks + user_ticks) / 10_000_000.0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def process_tree_cpu_sample(pid: int) -> Optional[Dict[str, Any]]:
    if os.name != "nt":
        return None
    pids = _windows_process_tree_pids(int(pid))
    total = 0.0
    observed: List[int] = []
    for tree_pid in pids:
        seconds = _windows_process_cpu_seconds(tree_pid)
        if seconds is not None:
            observed.append(tree_pid)
            total += seconds
    if not observed:
        return None
    return {"seconds": total, "pids": observed}


def closeout_recovery_command(closeout_args: Sequence[str], config: Optional[Dict[str, Any]] = None) -> str:
    command = str(closeout_args[0]) if closeout_args else "complete"
    script_by_command = {
        "start": "start-work-block.ps1",
        "bootstrap-response": "start-work-block.ps1",
        "complete": "work-block-complete.ps1",
        "detect": "detect-closeout.ps1",
        "repair": "repair-closeout.ps1",
        "checkpoint": "publish-checkpoint.ps1",
        "dirty-split": "repair-closeout.ps1",
        "finalize": "finalize-closeout.ps1",
        "review-quorum": "review-quorum.ps1",
        "orphan-quarantine": "orphan-quarantine.ps1",
        "audit": "audit-closeout.ps1",
        "sweep": "repo-sweep-closeout.ps1",
        "remediate-retained": "remediate-retained-closeout.ps1",
    }
    script = script_by_command.get(command, "work-block-complete.ps1")
    return closeout_script_command(script, config=config)


def parse_child_json(stdout: str) -> Optional[Dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def audit_entries(repo_root: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = audit_root(repo_root, config) / "audits.jsonl"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            with contextlib.suppress(json.JSONDecodeError):
                rows.append(json.loads(line))
    return rows


def closeout_success_artifact_exists(repo_root: Path, config: Dict[str, Any]) -> bool:
    return any(row.get("outcome") == "success" for row in audit_entries(repo_root, config))


def closeout_blocker_artifact_exists(repo_root: Path, config: Dict[str, Any]) -> bool:
    return any(row.get("outcome") in {"blocked", "retained"} for row in audit_entries(repo_root, config))


def closeout_command_requires_success_artifact(closeout_args: Sequence[str], child_json: Optional[Dict[str, Any]]) -> bool:
    command = str(closeout_args[0]) if closeout_args else ""
    status = str((child_json or {}).get("status") or "")
    if child_json and child_json.get("eligible") is False:
        return False
    if command == "finalize" or (command == "complete" and "--finalize" in closeout_args):
        return status == "success"
    if command in {"detect", "repair", "checkpoint", "dirty-split", "sweep", "orphan-quarantine", "remediate-retained"}:
        return status in {"", "success"}
    return False


def closeout_command_requires_blocker_artifact(closeout_args: Sequence[str], child_json: Optional[Dict[str, Any]]) -> bool:
    if not child_json:
        return False
    status = str(child_json.get("status") or "")
    if status in {"blocked", "failed", "error"}:
        return True
    command = str(closeout_args[0]) if closeout_args else ""
    return command == "detect" and child_json.get("eligible") is False


def closeout_command_has_semantic_success_authority(closeout_args: Sequence[str], child_json: Optional[Dict[str, Any]]) -> bool:
    if not child_json:
        return False
    if str(child_json.get("status") or "") != "success":
        return False
    command = str(closeout_args[0]) if closeout_args else ""
    return command == "finalize" or (command == "complete" and "--finalize" in closeout_args)


def normalize_closeout_child_status(
    repo_root: Path,
    config: Dict[str, Any],
    result: Dict[str, Any],
    *,
    closeout_args: Optional[Sequence[str]] = None,
    expected_success_artifact: Optional[Path] = None,
    expected_blocker_artifact: Optional[Path] = None,
    normalize_failure_text: bool = True,
) -> Dict[str, Any]:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    combined = ("%s\n%s" % (stdout, stderr)).lower()
    child_json = parse_child_json(stdout)
    status_value = str((child_json or {}).get("status") or "")
    json_blocked = status_value in {"blocked", "failed", "error"}
    if child_json and child_json.get("eligible") is False:
        json_blocked = True
    original_returncode = int(result.get("returncode") or 0)
    args = list(closeout_args or [])
    raw_matched = [pattern for pattern in closeout_failure_text_patterns(config) if pattern in combined] if normalize_failure_text else []
    semantic_success_authority = (
        original_returncode == 0
        and normalize_failure_text
        and closeout_command_has_semantic_success_authority(args, child_json)
    )
    matched = [] if semantic_success_authority else raw_matched
    ignored_matched = raw_matched if semantic_success_authority else []
    normalized_reasons: List[str] = []
    if matched:
        normalized_reasons.append("known_failure_text")
    if json_blocked:
        normalized_reasons.append("json_status_failure")
    if expected_success_artifact is not None and original_returncode == 0 and not expected_success_artifact.exists():
        normalized_reasons.append("missing_success_artifact")
    if expected_blocker_artifact is not None and json_blocked and not expected_blocker_artifact.exists():
        normalized_reasons.append("missing_blocker_artifact")
    if args and original_returncode == 0:
        if closeout_command_requires_success_artifact(args, child_json) and not closeout_success_artifact_exists(repo_root, config):
            normalized_reasons.append("missing_success_artifact")
        if closeout_command_requires_blocker_artifact(args, child_json) and not closeout_blocker_artifact_exists(repo_root, config):
            normalized_reasons.append("missing_blocker_artifact")
    if result.get("timedOut"):
        return {"status": "timeout", "returncode": bounded_runner_exit_code(config, "timeout"), "matchedFailurePatterns": matched, "ignoredFailurePatterns": ignored_matched, "normalizedFailure": False, "normalizedReasons": []}
    if result.get("outputCapped"):
        return {"status": "output_cap", "returncode": bounded_runner_exit_code(config, "output_cap"), "matchedFailurePatterns": matched, "ignoredFailurePatterns": ignored_matched, "normalizedFailure": False, "normalizedReasons": []}
    if result.get("cpuStalled"):
        return {"status": "cpu_stall", "returncode": bounded_runner_exit_code(config, "cpu_stall"), "matchedFailurePatterns": matched, "ignoredFailurePatterns": ignored_matched, "normalizedFailure": False, "normalizedReasons": []}
    if original_returncode == 0 and normalized_reasons:
        return {
            "status": "normalized_failure",
            "returncode": 20,
            "matchedFailurePatterns": matched,
            "ignoredFailurePatterns": ignored_matched,
            "normalizedFailure": True,
            "normalizedReasons": sorted(set(normalized_reasons)),
            "childStatus": status_value or None,
        }
    return {
        "status": "success" if original_returncode == 0 else "failed",
        "returncode": original_returncode,
        "matchedFailurePatterns": matched,
        "ignoredFailurePatterns": ignored_matched,
        "normalizedFailure": False,
        "normalizedReasons": [],
        "childStatus": status_value or None,
    }


def kill_process_tree(pid: int) -> Dict[str, Any]:
    if pid <= 0:
        return {"processTreeKillAttempted": False, "killedDescendants": [], "reason": "invalid_pid"}
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        text = "%s\n%s" % (completed.stdout or "", completed.stderr or "")
        killed = sorted({int(value) for value in re.findall(r"PID\s+(\d+)", text) if int(value) != pid})
        return {
            "processTreeKillAttempted": True,
            "method": "taskkill",
            "returncode": completed.returncode,
            "killedDescendants": killed,
            "stdout": (completed.stdout or "")[-2000:],
            "stderr": (completed.stderr or "")[-2000:],
        }
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
        return {"processTreeKillAttempted": True, "method": "killpg", "returncode": 0, "killedDescendants": [], "processGroup": pgid}
    except ProcessLookupError:
        return {"processTreeKillAttempted": True, "method": "killpg", "returncode": 0, "killedDescendants": [], "alreadyExited": True}
    except Exception as exc:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        return {"processTreeKillAttempted": True, "method": "kill", "returncode": 1, "killedDescendants": [], "error": str(exc)}


def _bounded_stream_reader(stream: Any, label: str, state: Dict[str, Any], lock: threading.Lock, stop_event: threading.Event, max_bytes: int) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            with lock:
                state[label + "Bytes"] += len(chunk)
                remaining = max(0, max_bytes - state["storedBytes"])
                if remaining:
                    state[label].extend(chunk[:remaining])
                    state["storedBytes"] += min(len(chunk), remaining)
                if state["stdoutBytes"] + state["stderrBytes"] > max_bytes:
                    state["outputCapped"] = True
                    stop_event.set()
                    break
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def run_bounded_closeout_process(
    repo_root_arg: Path,
    config: Dict[str, Any],
    command: Sequence[str],
    *,
    timeout_ms: int,
    max_output_bytes: int,
    recovery_command: str,
    closeout_args: Optional[Sequence[str]] = None,
    work_block_id: Optional[str] = None,
    expected_success_artifact: Optional[Path] = None,
    expected_blocker_artifact: Optional[Path] = None,
    normalize_failure_text: bool = True,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
    resource_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    process_cwd = cwd or repo_root
    timeout_ms = positive_int(timeout_ms, 120000)
    max_output_bytes = positive_int(max_output_bytes, 1048576)
    started_at = utc_now()
    resource_policy = closeout_process_resource_policy(config, closeout_args=closeout_args, command_config=resource_overrides)
    popen_kwargs: Dict[str, Any] = {
        "cwd": str(process_cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "start_new_session": (os.name != "nt"),
    }
    if os.name == "nt" and int(resource_policy.get("priorityCreationFlags") or 0):
        popen_kwargs["creationflags"] = int(resource_policy["priorityCreationFlags"])
    process = subprocess.Popen(list(command), **popen_kwargs)
    affinity_result = apply_windows_process_affinity(process, resource_policy.get("affinityMask"))
    tree_affinity_result = apply_windows_process_tree_affinity(process.pid, resource_policy.get("affinityMask"))
    state: Dict[str, Any] = {
        "stdout": bytearray(),
        "stderr": bytearray(),
        "stdoutBytes": 0,
        "stderrBytes": 0,
        "storedBytes": 0,
        "outputCapped": False,
    }
    lock = threading.Lock()
    stop_event = threading.Event()
    threads = [
        threading.Thread(target=_bounded_stream_reader, args=(process.stdout, "stdout", state, lock, stop_event, max_output_bytes), daemon=True),
        threading.Thread(target=_bounded_stream_reader, args=(process.stderr, "stderr", state, lock, stop_event, max_output_bytes), daemon=True),
    ]
    for thread in threads:
        thread.start()
    reason: Optional[str] = None
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    watchdog_policy = dict(resource_policy.get("cpuWatchdog") or {})
    cpu_watchdog_state: Dict[str, Any] = {
        "enabled": bool(watchdog_policy.get("enabled", False)),
        "thresholdPercent": watchdog_policy.get("thresholdPercent"),
        "sustainedSeconds": watchdog_policy.get("sustainedSeconds"),
        "sampleIntervalSeconds": watchdog_policy.get("sampleIntervalSeconds"),
        "requireNoOutputProgress": watchdog_policy.get("requireNoOutputProgress"),
        "supported": os.name == "nt",
        "lastCpuPercent": None,
        "lastObservedPids": [],
        "lastOutputBytes": 0,
        "sustainedHighCpuSeconds": 0.0,
    }
    last_cpu_sample = process_tree_cpu_sample(process.pid) if cpu_watchdog_state["enabled"] else None
    last_cpu_sample_at = time.monotonic()
    last_output_sample = 0
    high_cpu_since: Optional[float] = None
    next_cpu_sample_at = last_cpu_sample_at + positive_float(watchdog_policy.get("sampleIntervalSeconds"), 2.0)
    while process.poll() is None:
        if stop_event.is_set():
            reason = "output_cap"
            break
        now = time.monotonic()
        if cpu_watchdog_state["enabled"] and now >= next_cpu_sample_at:
            current_cpu_sample = process_tree_cpu_sample(process.pid)
            with lock:
                output_bytes = int(state["stdoutBytes"] + state["stderrBytes"])
            if current_cpu_sample is None:
                cpu_watchdog_state["sampleUnavailable"] = True
                high_cpu_since = None
            elif last_cpu_sample is not None:
                if resource_policy.get("affinityMask"):
                    tree_affinity_result = apply_windows_process_tree_affinity(process.pid, resource_policy.get("affinityMask"))
                delta_cpu = max(0.0, float(current_cpu_sample["seconds"]) - float(last_cpu_sample["seconds"]))
                delta_wall = max(0.001, now - last_cpu_sample_at)
                cpu_percent = (delta_cpu / delta_wall) * 100.0
                output_progress = output_bytes > last_output_sample
                require_no_output = bool(watchdog_policy.get("requireNoOutputProgress", True))
                threshold = positive_float(watchdog_policy.get("thresholdPercent"), 80.0)
                cpu_watchdog_state.update(
                    {
                        "lastCpuPercent": round(cpu_percent, 2),
                        "lastObservedPids": list(current_cpu_sample.get("pids", [])),
                        "lastOutputBytes": output_bytes,
                        "outputProgress": output_progress,
                    }
                )
                if cpu_percent >= threshold and (not require_no_output or not output_progress):
                    if high_cpu_since is None:
                        high_cpu_since = now
                    sustained = now - high_cpu_since
                    cpu_watchdog_state["sustainedHighCpuSeconds"] = round(sustained, 3)
                    if sustained >= positive_float(watchdog_policy.get("sustainedSeconds"), 30.0):
                        reason = "cpu_stall"
                        break
                else:
                    high_cpu_since = None
                    cpu_watchdog_state["sustainedHighCpuSeconds"] = 0.0
            last_cpu_sample = current_cpu_sample
            last_cpu_sample_at = now
            last_output_sample = output_bytes
            next_cpu_sample_at = now + positive_float(watchdog_policy.get("sampleIntervalSeconds"), 2.0)
        if time.monotonic() >= deadline:
            reason = "timeout"
            break
        time.sleep(0.02)
    kill_info: Dict[str, Any] = {"processTreeKillAttempted": False, "killedDescendants": []}
    if reason:
        kill_info = kill_process_tree(process.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=2)
    else:
        process.wait()
    for thread in threads:
        thread.join(timeout=2)
    stdout = bytes(state["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(state["stderr"]).decode("utf-8", errors="replace")
    raw_returncode = process.returncode
    if reason in BOUNDED_RUNNER_STATUS_EXIT_CODE_KEYS:
        raw_returncode = bounded_runner_exit_code(config, reason)
    elif raw_returncode is None:
        raw_returncode = 1
    base_result: Dict[str, Any] = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "status": "pending",
        "args": list(command),
        "cwd": str(process_cwd),
        "pid": process.pid,
        "returncode": int(raw_returncode),
        "startedAt": started_at,
        "completedAt": utc_now(),
        "timeoutMs": timeout_ms,
        "maxOutputBytes": max_output_bytes,
        "exitCodePolicy": bounded_runner_exit_codes(config),
        "stdout": stdout,
        "stderr": stderr,
        "stdoutBytes": int(state["stdoutBytes"]),
        "stderrBytes": int(state["stderrBytes"]),
        "timedOut": reason == "timeout",
        "outputCapped": bool(state["outputCapped"] or reason == "output_cap"),
        "cpuStalled": reason == "cpu_stall",
        "cpuWatchdog": cpu_watchdog_state,
        "resourcePolicy": resource_policy,
        "resourceApply": {"affinity": affinity_result, "treeAffinity": tree_affinity_result},
        "killedProcessTree": bool(kill_info.get("processTreeKillAttempted")),
        "killedDescendants": kill_info.get("killedDescendants", []),
        "kill": kill_info,
        "recoveryCommand": recovery_command,
    }
    normalized = normalize_closeout_child_status(
        repo_root,
        config,
        base_result,
        closeout_args=closeout_args,
        expected_success_artifact=expected_success_artifact,
        expected_blocker_artifact=expected_blocker_artifact,
        normalize_failure_text=normalize_failure_text,
    )
    base_result.update(normalized)
    audit_payload = {k: v for k, v in base_result.items() if k not in {"stdout", "stderr"}}
    audit_payload["stdoutTail"] = stdout[-4000:]
    audit_payload["stderrTail"] = stderr[-4000:]
    if base_result["timedOut"]:
        write_audit(repo_root, config, "bounded_runner_timeout", audit_payload, work_block_id=work_block_id, outcome="blocked")
    if base_result["outputCapped"]:
        write_audit(repo_root, config, "bounded_runner_output_cap", audit_payload, work_block_id=work_block_id, outcome="blocked")
    if base_result["cpuStalled"]:
        write_audit(repo_root, config, "bounded_runner_cpu_stall", audit_payload, work_block_id=work_block_id, outcome="blocked")
    if base_result["killedProcessTree"]:
        write_audit(repo_root, config, "bounded_runner_process_tree_killed", audit_payload, work_block_id=work_block_id, outcome="blocked")
    if base_result["normalizedFailure"]:
        write_audit(repo_root, config, "bounded_runner_normalized_failure", audit_payload, work_block_id=work_block_id, outcome="blocked")
    return base_result


def bounded_closeout_run(repo_root_arg: Path, closeout_args: Sequence[str]) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    args = list(closeout_args)
    command = [
        sys.executable,
        "-m",
        "tools.repo_hygiene.work_block_cli",
        "--repo-root",
        ".",
        *args,
    ]
    return run_bounded_closeout_process(
        repo_root,
        config,
        command,
        timeout_ms=closeout_command_timeout_ms(config, args),
        max_output_bytes=closeout_max_process_output_bytes(config),
        recovery_command=closeout_recovery_command(args, config),
        closeout_args=args,
    )


def bounded_closeout_cli_main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    repo_root = "."
    if "--repo-root" in args:
        index = args.index("--repo-root")
        if index + 1 >= len(args):
            print("bounded closeout runner error: --repo-root requires a value", file=sys.stderr)
            return 64
        repo_root = args[index + 1]
        del args[index : index + 2]
    if "--" in args:
        args = args[args.index("--") + 1 :]
    if not args:
        print("bounded closeout runner error: missing closeout command", file=sys.stderr)
        return 64
    result = bounded_closeout_run(Path(repo_root), args)
    if result.get("stdout"):
        sys.stdout.write(str(result["stdout"]))
        if not str(result["stdout"]).endswith("\n"):
            sys.stdout.write("\n")
    if result.get("stderr"):
        sys.stderr.write(str(result["stderr"]))
        if not str(result["stderr"]).endswith("\n"):
            sys.stderr.write("\n")
    if result["status"] != "success":
        summary = {k: v for k, v in result.items() if k not in {"stdout", "stderr"}}
        print(json.dumps({"boundedRunner": summary}, indent=2, sort_keys=True), file=sys.stderr)
    return int(result["returncode"])


def start_work_block(
    repo_root_arg: Path,
    *,
    work_block_id: Optional[str] = None,
    actor: str = "codex",
    path_claims: Optional[Sequence[str]] = None,
    lease_seconds: int = 3600,
    summary: Optional[str] = None,
    start_head: Optional[str] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    assert_remediation_freeze_not_active(repo_root, config, action="start-work-block")
    block_id = work_block_id or ("wb-%s" % uuid.uuid4().hex[:16])
    commit_subject = normalize_commit_subject(summary)
    if summary and not commit_subject:
        raise HygieneError("work block summary must be a human-readable commit subject")
    branch = current_branch(repo_root)
    if not branch:
        raise HygieneError("work block start requires a named branch")
    claims = sorted({normalize_rel(path) for path in path_claims or [] if normalize_rel(str(path))})
    with broker_lease(repo_root, config, "broker", lease_seconds=30):
        block_dir = work_block_dir(repo_root, config, block_id)
        if (block_dir / "manifest.json").exists():
            raise HygieneError("work block already exists: %s" % block_id)
        protected_bootstrap = None
        if is_protected_branch(config, branch):
            protected_bootstrap = auto_branch_from_protected_target(repo_root, config, branch, block_id)
            branch = protected_bootstrap["createdBranch"]
        head = rev_parse(repo_root, "HEAD")
        manifest_start_head, start_head_override = resolve_work_block_start_head(
            repo_root, config, start_head, head
        )
        dirty_baseline = dirty_baseline_snapshot(repo_root)
        manifest = {
            "schemaVersion": BROKER_SCHEMA_VERSION,
            "workBlockId": block_id,
            "state": "active",
            "actor": actor,
            "branch": branch,
            "worktree": str(repo_root),
            "targetBranch": config.get("git", {}).get("targetBranch", "master"),
            "targetRemote": config.get("git", {}).get("remote", "origin"),
            "pathClaims": claims,
            "startedAt": utc_now(),
            "updatedAt": utc_now(),
            "startedAtPrecise": utc_now_precise(),
            "updatedAtPrecise": utc_now_precise(),
            "lease": {
                "holder": actor,
                "seconds": lease_seconds,
                "createdAt": utc_now(),
            },
            "startHead": manifest_start_head,
            "dirtyBaseline": dirty_baseline,
        }
        if start_head_override:
            manifest["startHeadOverride"] = start_head_override
        if commit_subject:
            manifest["commitSubject"] = commit_subject
        if protected_bootstrap:
            manifest["protectedBranchBootstrap"] = protected_bootstrap
        write_json(block_dir / "manifest.json", manifest)
        append_event(
            repo_root,
            config,
            block_id,
            {
                "event": "work_block_started",
                "branch": branch,
                "head": head,
                "startHead": manifest_start_head,
                "startHeadOverride": start_head_override,
                "pathClaims": claims,
                "dirtyBaselinePaths": dirty_baseline["paths"],
                "commitSubject": commit_subject or None,
                "protectedBranchBootstrap": protected_bootstrap,
            },
        )
    return {"status": "started", "workBlockId": block_id, "manifest": manifest}


def recover_dirty_protected_target_to_work_block(
    repo_root: Path,
    config: Dict[str, Any],
    protected_branch: str,
    *,
    actor: str = "codex-protected-target-recovery",
    path_claims: Optional[Sequence[str]] = None,
    lease_seconds: int = 3600,
) -> Dict[str, Any]:
    dirty_before = dirty_baseline_snapshot(repo_root)
    if not dirty_before["paths"]:
        raise HygieneError("dirty protected target recovery requires dirty paths")
    block_id = "wb-%s" % uuid.uuid4().hex[:16]
    effective_claims = list(path_claims or dirty_before["paths"])
    claims = sorted({normalize_rel(path) for path in effective_claims if normalize_rel(str(path))})
    with broker_lease(repo_root, config, "broker", lease_seconds=30):
        block_dir = work_block_dir(repo_root, config, block_id)
        if (block_dir / "manifest.json").exists():
            raise HygieneError("work block already exists: %s" % block_id)
        branch = work_block_bootstrap_branch(config, block_id)
        if rev_parse(repo_root, f"refs/heads/{branch}", required=False):
            raise HygieneError("work block bootstrap branch already exists: %s" % branch)
        start_head = rev_parse(repo_root, "HEAD")
        checkout = run_git(repo_root, ["checkout", "-b", branch])
        if checkout.returncode != 0:
            raise HygieneError("failed to recover dirty protected target onto %s: %s" % (branch, checkout.stderr.strip()))
        head = rev_parse(repo_root, "HEAD")
        dirty_baseline = dirty_baseline_snapshot(repo_root)
        protected_bootstrap = {
            "enabled": True,
            "fromProtectedBranch": protected_branch,
            "createdBranch": branch,
            "startHead": start_head,
            "reason": "protected_branch_dirty_recovery",
            "dirtyPaths": dirty_before["paths"],
        }
        manifest = {
            "schemaVersion": BROKER_SCHEMA_VERSION,
            "workBlockId": block_id,
            "state": "active",
            "actor": actor,
            "branch": branch,
            "worktree": str(repo_root),
            "targetBranch": config.get("git", {}).get("targetBranch", "master"),
            "targetRemote": config.get("git", {}).get("remote", "origin"),
            "pathClaims": claims,
            "startedAt": utc_now(),
            "updatedAt": utc_now(),
            "startedAtPrecise": utc_now_precise(),
            "updatedAtPrecise": utc_now_precise(),
            "lease": {
                "holder": actor,
                "seconds": lease_seconds,
                "createdAt": utc_now(),
            },
            "startHead": head,
            "dirtyBaseline": dirty_baseline,
            "protectedBranchBootstrap": protected_bootstrap,
        }
        write_json(block_dir / "manifest.json", manifest)
        append_event(
            repo_root,
            config,
            block_id,
            {
                "event": "protected_target_dirty_recovered",
                "fromProtectedBranch": protected_branch,
                "branch": branch,
                "head": head,
                "pathClaims": claims,
                "dirtyBaselinePaths": dirty_baseline["paths"],
                "protectedBranchBootstrap": protected_bootstrap,
            },
        )
    return {"status": "recovered", "workBlockId": block_id, "manifest": manifest}


def update_manifest(repo_root: Path, config: Dict[str, Any], work_block_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    manifest = load_manifest(repo_root, config, work_block_id)
    manifest.update(updates)
    manifest["updatedAt"] = utc_now()
    manifest["updatedAtPrecise"] = utc_now_precise()
    write_json(work_block_dir(repo_root, config, work_block_id) / "manifest.json", manifest)
    return manifest


def closeout_tooling_recovery_command() -> str:
    return "git status --short && git checkout <tooling-baseline> -- closeout.config.json tools/closeout tools/repo_hygiene tools/repo-hygiene && rerun closeout"


def baseline_ref_for_tooling(repo_root: Path, config: Dict[str, Any]) -> Optional[str]:
    baseline = config.get("toolingBaseline", {})
    configured = baseline.get("baselineRef")
    if configured:
        return str(configured)
    try:
        target = target_ref_for(repo_root, config)
    except HygieneError:
        return None
    return str(target["ref"])


def git_show_text(repo_root: Path, ref: str, path: str) -> Optional[str]:
    result = run_git(repo_root, ["show", f"{ref}:{path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def dirty_path_set(repo_root: Path) -> set[str]:
    return {entry["path"] for entry in parse_status_paths(repo_root)}


def can_update_tooling_path(repo_root: Path, config: Dict[str, Any], path: str, dirty_paths: set[str], claims: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    normalized = normalize_rel(path)
    if normalized in dirty_paths:
        return False, "path_has_dirty_work"
    if normalized in claims:
        return False, "path_is_claimed_by_work_block:%s" % claims[normalized]
    local = repo_root / normalized
    if local.exists() and not local.is_file():
        return False, "path_is_not_regular_file"
    return True, None


def repository_has_canonical_acceptance_remote(repo_root: Path) -> bool:
    result = run_git(repo_root, ["config", "--get-regexp", r"^remote\..*\.url$"])
    if result.returncode != 0:
        return False
    canonical_urls = {
        "https://github.com/layibabalola/mlv-app",
        "git@github.com:layibabalola/mlv-app",
        "ssh://git@github.com/layibabalola/mlv-app",
    }
    for line in result.stdout.splitlines():
        _, _, raw_url = line.partition(" ")
        normalized = raw_url.strip().lower().rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        if normalized in canonical_urls:
            return True
    return False


def verify_closeout_tooling_current(
    repo_root_arg: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    attempt_update: bool = True,
    plan_only: bool = False,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = config or load_closeout_config(repo_root)
    raw_config = read_json(repo_root / CONFIG_PATH, {}) if (repo_root / CONFIG_PATH).exists() else {}
    baseline = config.get("toolingBaseline", {})
    raw_acceptance = raw_config.get("candidateAcceptance") if isinstance(raw_config, dict) else None
    acceptance_bearing = (
        isinstance(raw_acceptance, dict) and raw_acceptance.get("schema") == "candidate-acceptance.v1"
    ) or repository_has_canonical_acceptance_remote(repo_root)
    if not bool(baseline.get("enabled", False)) and not acceptance_bearing:
        return {"ok": True, "status": "disabled", "missing": [], "updated": [], "plannedUpdates": []}

    missing: List[Dict[str, Any]] = []
    if acceptance_bearing and baseline.get("enabled") is not True:
        missing.append({"kind": "baseline_enabled", "key": "toolingBaseline.enabled", "expected": True})
    default_baseline = DEFAULT_CLOSEOUT_CONFIG["toolingBaseline"]
    mandatory_paths = {
        "tools/repo_hygiene/candidate_acceptance.py",
        "tools/repo_hygiene/test_candidate_acceptance.py",
        "tools/factory/capture-github-acceptance.ps1",
    }
    configured_paths = {normalize_rel(str(path)) for path in baseline.get("paths", [])}
    for path in sorted(mandatory_paths - configured_paths):
        missing.append({"kind": "baseline_path", "path": path})

    mandatory_config_keys = {
        str(key)
        for key in default_baseline.get("requiredConfigKeys", [])
        if str(key).startswith("candidateAcceptance") or str(key) == "toolingBaseline.requiredTestFiles"
    }
    mandatory_config_keys.add("repoSweep.maxNewInvestigationsPerRun")
    configured_keys = {str(key) for key in baseline.get("requiredConfigKeys", [])}
    for key in sorted(mandatory_config_keys - configured_keys):
        missing.append({"kind": "baseline_config_requirement", "key": key})

    mandatory_test_files = {
        (normalize_rel(str(item.get("path") or "")), str(item.get("test") or ""))
        for item in default_baseline.get("requiredTestFiles", [])
        if isinstance(item, dict)
    }
    configured_test_files = {
        (normalize_rel(str(item.get("path") or "")), str(item.get("test") or ""))
        for item in baseline.get("requiredTestFiles", [])
        if isinstance(item, dict)
    }
    for path, test_name in sorted(mandatory_test_files - configured_test_files):
        missing.append({"kind": "baseline_test_requirement", "path": path, "test": test_name})

    mandatory_symbols = {
        (normalize_rel(str(item.get("path") or "")), str(item.get("contains") or ""))
        for item in default_baseline.get("requiredSymbols", [])
        if isinstance(item, dict)
        and (
            normalize_rel(str(item.get("path") or "")) == "tools/repo_hygiene/candidate_acceptance.py"
            or str(item.get("contains") or "")
            in {
                "def integration_range_evidence",
                "def repository_has_canonical_acceptance_remote",
                "def repo_sweep_plan",
                "def repo_sweep_tuple",
                "def reusable_candidate_report",
                "def investigation_jobs",
                "def fresh_investigation_report",
                "def remote_provider_ref_snapshot",
                "def delete_remote_feature_ref",
            }
        )
    }
    configured_symbols = {
        (normalize_rel(str(item.get("path") or "")), str(item.get("contains") or ""))
        for item in baseline.get("requiredSymbols", [])
        if isinstance(item, dict)
    }
    for path, contains in sorted(mandatory_symbols - configured_symbols):
        missing.append({"kind": "baseline_symbol_requirement", "path": path, "contains": contains})

    for key in baseline.get("requiredConfigKeys", []):
        if config_path_value(raw_config, str(key)) is None:
            missing.append({"kind": "config_key", "key": str(key)})
    high_impact = set(config.get("reviewQuorum", {}).get("highImpactActions", []))
    for action in baseline.get("requiredHighImpactActions", []):
        if action not in high_impact:
            missing.append({"kind": "high_impact_action", "action": str(action)})
    autonomous = set(config.get("autoQuorum", {}).get("autonomousActionClasses", []))
    for action in baseline.get("requiredAutoQuorumActions", []):
        if action not in autonomous:
            missing.append({"kind": "auto_quorum_action", "action": str(action)})
    test_path = repo_root / "tools" / "repo_hygiene" / "test_brokered_closeout.py"
    test_text = test_path.read_text(encoding="utf-8") if test_path.exists() else ""
    for test_name in baseline.get("requiredTests", []):
        if str(test_name) not in test_text:
            missing.append({"kind": "test", "test": str(test_name), "path": normalize_rel(str(test_path.relative_to(repo_root))) if test_path.exists() else "tools/repo_hygiene/test_brokered_closeout.py"})
    for required_test in baseline.get("requiredTestFiles", []):
        if not isinstance(required_test, dict):
            missing.append({"kind": "test", "test": str(required_test), "path": ""})
            continue
        path = normalize_rel(str(required_test.get("path") or ""))
        test_name = str(required_test.get("test") or "")
        candidate_path = repo_root / path
        try:
            candidate_path.resolve().relative_to(repo_root.resolve())
        except (OSError, ValueError):
            missing.append({"kind": "test", "test": test_name, "path": path})
            continue
        candidate_text = candidate_path.read_text(encoding="utf-8") if path and candidate_path.is_file() else ""
        if not path or not test_name or test_name not in candidate_text:
            missing.append({"kind": "test", "test": test_name, "path": path})
    for required in baseline.get("requiredSymbols", []):
        path = normalize_rel(str(required.get("path") or ""))
        contains = str(required.get("contains") or "")
        text = (repo_root / path).read_text(encoding="utf-8") if path and (repo_root / path).exists() else ""
        if not path or contains not in text:
            missing.append({"kind": "symbol", "path": path, "contains": contains})
    for script in REQUIRED_SCRIPT_NAMES:
        if not (repo_root / "tools" / "closeout" / script).exists():
            missing.append({"kind": "actor", "path": normalize_rel(f"tools/closeout/{script}")})

    updated: List[Dict[str, Any]] = []
    planned_updates: List[Dict[str, Any]] = []
    blocked_updates: List[Dict[str, Any]] = []
    if missing and bool(baseline.get("autoUpdate", True)):
        ref = baseline_ref_for_tooling(repo_root, config)
        dirty_paths = dirty_path_set(repo_root)
        claims = active_path_claims(repo_root, config)
        candidate_paths = sorted({normalize_rel(str(item.get("path") or item.get("key") or "")) for item in missing if item.get("path")})
        candidate_paths.extend(normalize_rel(str(path)) for path in baseline.get("paths", []) if normalize_rel(str(path)))
        for path in sorted({path for path in candidate_paths if path}):
            allowed, reason = can_update_tooling_path(repo_root, config, path, dirty_paths, claims)
            if not allowed:
                blocked_updates.append({"path": path, "reason": reason})
                continue
            if not ref:
                blocked_updates.append({"path": path, "reason": "no_tooling_baseline_ref"})
                continue
            content = git_show_text(repo_root, ref, path)
            if content is None:
                blocked_updates.append({"path": path, "reason": "path_missing_from_baseline", "baselineRef": ref})
                continue
            update = {"path": path, "baselineRef": ref}
            if plan_only or not attempt_update:
                planned_updates.append(update)
                continue
            target = repo_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
            updated.append(update)

    result = {
        "ok": not missing,
        "status": "current" if not missing else "closeout_tooling_stale",
        "missing": missing,
        "updated": updated,
        "plannedUpdates": planned_updates,
        "blockedUpdates": blocked_updates,
        "planOnly": bool(plan_only),
        "recoveryCommand": closeout_tooling_recovery_command(),
        "authoritative": not missing,
    }
    if missing:
        write_audit(repo_root, config, "closeout_tooling_stale", result, outcome="blocked")
    return result


def branch_work_block_candidates(repo_root: Path, config: Dict[str, Any], branch: str) -> List[Dict[str, Any]]:
    root = work_blocks_root(repo_root, config)
    candidates: List[Dict[str, Any]] = []
    for manifest_file in sorted(root.glob("*/manifest.json")) if root.exists() else []:
        manifest = read_json(manifest_file, {})
        if manifest.get("branch") == branch and manifest.get("state") in {"active", "completed", "finalizing", "blocked"}:
            candidates.append(manifest)
    return candidates


def bootstrap_response_broker_manifest(
    repo_root_arg: Path,
    *,
    hook_phase: str = "response",
    actor: str = "codex-response-hook",
    path_claims: Optional[Sequence[str]] = None,
    lease_seconds: int = 3600,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action=f"{hook_phase}-hook", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "skipped", "reason": "remediation_freeze", "freeze": freeze}
    branch = current_branch(repo_root)
    if not branch:
        return {"status": "skipped", "reason": "detached_head"}
    if is_protected_branch(config, branch):
        return {"status": "skipped", "reason": "protected_branch", "branch": branch}
    claims = sorted({normalize_rel(path) for path in path_claims or [] if normalize_rel(str(path))})
    with broker_lease(repo_root, config, "broker", lease_seconds=30):
        candidates = branch_work_block_candidates(repo_root, config, branch)
        if candidates:
            selected = max(candidates, key=work_block_selection_key)
            block_id = str(selected.get("workBlockId") or "")
            updates: Dict[str, Any] = {
                "actor": selected.get("actor") or actor,
                "branch": branch,
                "worktree": str(repo_root),
                "lease": {"holder": actor, "seconds": lease_seconds, "createdAt": utc_now()},
                "responseBroker": {"hookPhase": hook_phase, "refreshedAt": utc_now()},
            }
            if claims:
                updates["pathClaims"] = sorted(set(manifest_path_claims(selected)).union(claims))
            if not manifest_has_dirty_baseline(selected):
                updates["dirtyBaseline"] = dirty_baseline_snapshot(repo_root)
            manifest = update_manifest(repo_root, config, block_id, updates)
            append_event(
                repo_root,
                config,
                block_id,
                {
                    "event": "response_broker_refreshed",
                    "hookPhase": hook_phase,
                    "selection": work_block_selection_summary(manifest, "selected_by_branch_state_updated_workBlockId", len(candidates)),
                    "dirtyBaselinePaths": manifest.get("dirtyBaseline", {}).get("paths", []),
                },
            )
            return {
                "status": "refreshed",
                "workBlockId": block_id,
                "manifest": manifest,
                "workBlockSelection": work_block_selection_summary(manifest, "selected_by_branch_state_updated_workBlockId", len(candidates)),
            }
        block_id = "wb-%s" % uuid.uuid4().hex[:16]
        head = rev_parse(repo_root, "HEAD")
        dirty_baseline = dirty_baseline_snapshot(repo_root)
        now = utc_now()
        now_precise = utc_now_precise()
        manifest = {
            "schemaVersion": BROKER_SCHEMA_VERSION,
            "workBlockId": block_id,
            "state": "active",
            "actor": actor,
            "branch": branch,
            "worktree": str(repo_root),
            "targetBranch": config.get("git", {}).get("targetBranch", "master"),
            "targetRemote": config.get("git", {}).get("remote", "origin"),
            "pathClaims": claims,
            "startedAt": now,
            "updatedAt": now,
            "startedAtPrecise": now_precise,
            "updatedAtPrecise": now_precise,
            "lease": {"holder": actor, "seconds": lease_seconds, "createdAt": now},
            "startHead": head,
            "dirtyBaseline": dirty_baseline,
            "responseBroker": {"hookPhase": hook_phase, "createdAt": now},
        }
        write_json(work_block_dir(repo_root, config, block_id) / "manifest.json", manifest)
        append_event(
            repo_root,
            config,
            block_id,
            {
                "event": "response_broker_bootstrapped",
                "hookPhase": hook_phase,
                "branch": branch,
                "head": head,
                "pathClaims": claims,
                "dirtyBaselinePaths": dirty_baseline["paths"],
            },
        )
        return {
            "status": "created",
            "workBlockId": block_id,
            "manifest": manifest,
            "workBlockSelection": work_block_selection_summary(manifest, "created_response_broker_manifest", 0),
        }


def ensure_work_block_for_current_branch(repo_root: Path, config: Dict[str, Any], work_block_id: Optional[str]) -> Dict[str, Any]:
    if work_block_id:
        return attach_work_block_selection(load_manifest(repo_root, config, work_block_id), "explicit_workBlockId", 1)
    branch = current_branch(repo_root)
    if branch:
        candidates = branch_work_block_candidates(repo_root, config, branch)
        if candidates:
            return attach_work_block_selection(max(candidates, key=work_block_selection_key), "selected_by_branch_state_updated_workBlockId", len(candidates))
    implicit = start_work_block(repo_root, actor="codex-implicit", path_claims=[])
    return attach_work_block_selection(implicit["manifest"], "created_implicit_work_block", 0)


def detect_work_block(repo_root_arg: Path, *, work_block_id: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    manifest = ensure_work_block_for_current_branch(repo_root, config, work_block_id)
    block_id = str(manifest["workBlockId"])
    branch = str(manifest.get("branch") or current_branch(repo_root) or "")
    feature_head = rev_parse(repo_root, branch)
    target = target_ref_for(repo_root, config)
    delta_paths = changed_paths_between(repo_root, target["ref"], feature_head)
    dirty_entries = parse_status_paths(repo_root)
    dirty_paths = sorted({entry["path"] for entry in dirty_entries})
    all_claims = active_path_claims(repo_root, config)
    own_claims = set(manifest_path_claims(manifest))
    baseline_paths = manifest_dirty_baseline_paths(manifest)
    baseline_entries = manifest_dirty_baseline_entries(manifest)
    baseline_available = manifest_has_dirty_baseline(manifest)
    protected_bootstrap = manifest.get("protectedBranchBootstrap") if isinstance(manifest.get("protectedBranchBootstrap"), dict) else {}
    protected_dirty_recovery = str(protected_bootstrap.get("reason") or "") == "protected_branch_dirty_recovery"
    protected_recovery_paths = {
        normalize_rel(path)
        for path in (protected_bootstrap.get("dirtyPaths") or [])
        if normalize_rel(str(path))
    }
    generated_patterns = config.get("paths", {}).get("generated", [])
    sensitive_patterns = config.get("paths", {}).get("sensitive", [])
    unclaimed_default = str(config.get("dirty", {}).get("unclaimedOutsideDelta", "foreign"))
    auto_claim_clean = bool(config.get("dirty", {}).get("autoClaimCleanAtStart", True))
    owned_dirty: List[Dict[str, Any]] = []
    foreign_dirty: List[Dict[str, Any]] = []
    unowned_dirty: List[Dict[str, Any]] = []
    mixed_dirty: List[Dict[str, Any]] = []
    for entry in dirty_entries:
        path = entry["path"]
        owner = all_claims.get(path)
        in_delta = path in delta_paths
        claimed_by_self = path in own_claims or owner == block_id
        dirty_at_baseline = path in baseline_paths
        baseline_details = dirty_baseline_change_details(repo_root, entry, baseline_entries.get(path)) if dirty_at_baseline else {}
        baseline_changed = bool(baseline_details.get("dirtyBaselineContentChanged"))
        generated = path_matches_any(path, generated_patterns)
        sensitive = path_matches_any(path, sensitive_patterns)
        sensitive_auto_claim_allowed = (
            sensitive
            and auto_claim_clean
            and baseline_available
            and not dirty_at_baseline
            and clean_at_start_sensitive_auto_claim_allowed(repo_root, config, path, entry)
        )
        enriched = {
            **entry,
            "ownerWorkBlockId": owner,
            "inCompletedBranchDelta": in_delta,
            "dirtyAtBrokerStart": dirty_at_baseline,
            "dirtyBaselineAvailable": baseline_available,
            "generated": generated,
            "sensitive": sensitive,
            "cleanAtStartSensitiveAutoClaimAllowed": sensitive_auto_claim_allowed,
            **baseline_details,
        }
        if protected_dirty_recovery and path in protected_recovery_paths:
            enriched["classificationReason"] = "protected target dirty recovery ownership proof"
            enriched["protectedTargetDirtyRecovery"] = True
            owned_dirty.append(enriched)
        elif owner and owner != block_id:
            enriched["classificationReason"] = "path is claimed by another work block"
            foreign_dirty.append(enriched)
        elif generated:
            enriched["classificationReason"] = "generated closeout/runtime path is not owned source work"
            foreign_dirty.append(enriched)
        elif dirty_at_baseline and (baseline_changed or in_delta or claimed_by_self):
            enriched["classificationReason"] = (
                "baseline dirty path changed after broker start"
                if baseline_changed and not (in_delta or claimed_by_self)
                else "baseline dirty path overlaps candidate delta or claim"
            )
            enriched["blocker"] = "baseline-dirty-overlaps-candidate"
            enriched["recoveryCommand"] = baseline_dirty_recovery_command([path])
            mixed_dirty.append(enriched)
        elif in_delta or claimed_by_self:
            enriched["classificationReason"] = "path overlaps completed branch delta or block claim"
            owned_dirty.append(enriched)
        elif sensitive_auto_claim_allowed:
            enriched["classificationReason"] = "tracked sensitive note was clean or absent at broker dirty baseline"
            enriched["autoClaimedByDirtyBaseline"] = True
            owned_dirty.append(enriched)
        elif sensitive and bool(config.get("dirty", {}).get("sensitiveUnownedBlocks", True)):
            enriched["classificationReason"] = "unclaimed sensitive path"
            unowned_dirty.append(enriched)
        elif auto_claim_clean and baseline_available and not dirty_at_baseline and not generated:
            enriched["classificationReason"] = "path was clean or absent at broker dirty baseline"
            enriched["autoClaimedByDirtyBaseline"] = True
            owned_dirty.append(enriched)
        elif unclaimed_default == "foreign":
            enriched["classificationReason"] = "path is outside completed branch delta"
            foreign_dirty.append(enriched)
        else:
            enriched["classificationReason"] = "path cannot be attributed"
            unowned_dirty.append(enriched)
    pinned_refs = {
        "feature": {"branch": branch, "head": feature_head},
        "target": {
            "branch": target["targetBranch"],
            "remote": target.get("remote"),
            "ref": target["ref"],
            "head": target["head"],
            "mode": target["mode"],
        },
    }
    result = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "workBlockId": block_id,
        "branch": branch,
        "targetBranch": target["targetBranch"],
        "targetRemote": target.get("remote"),
        "localOnly": target["mode"] == "local" and not target.get("remote"),
        "featureHead": feature_head,
        "targetHead": target["head"],
        "pinnedRefs": pinned_refs,
        "committedDeltaPaths": delta_paths,
        "dirtyPaths": dirty_paths,
        "dirtyBaselinePaths": sorted(baseline_paths),
        "ownedDirty": owned_dirty,
        "mixedDirty": mixed_dirty,
        "unownedDirty": unowned_dirty,
        "foreignDirty": foreign_dirty,
        "eligible": not owned_dirty and not unowned_dirty and not mixed_dirty,
        "workBlockSelection": manifest.get("workBlockSelection"),
    }
    detector_hash_payload = {key: value for key, value in result.items() if key != "workBlockSelection"}
    result["detectorHash"] = stable_hash(detector_hash_payload)
    write_json(work_block_dir(repo_root, config, block_id) / "detector.json", result)
    append_event(repo_root, config, block_id, {"event": "detector_ran", "detectorHash": result["detectorHash"], "eligible": result["eligible"]})
    write_audit(repo_root, config, "detector", result, work_block_id=block_id, outcome="success" if result["eligible"] else "blocked")
    return result


def upstream_for(repo_root: Path, branch: str) -> Optional[str]:
    result = run_git(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ahead_behind(repo_root: Path, upstream: str) -> Dict[str, int]:
    ahead = git_stdout(repo_root, ["rev-list", "--count", f"{upstream}..HEAD"], required=False)
    behind = git_stdout(repo_root, ["rev-list", "--count", f"HEAD..{upstream}"], required=False)
    return {"ahead": int(ahead or "0"), "behind": int(behind or "0")}


def evidence_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    root = normalize_rel(str(config.get("evidenceRepair", {}).get("evidenceRoot") or ".closeout-evidence"))
    if not root or root.startswith("../") or root == ".." or root.startswith(".claude/"):
        raise HygieneError("invalid evidenceRepair.evidenceRoot: %s" % root)
    return (repo_root / root).resolve()


def evidence_rel_paths(config: Dict[str, Any], work_block_id: str) -> List[str]:
    root = normalize_rel(str(config.get("evidenceRepair", {}).get("evidenceRoot") or ".closeout-evidence")).strip("/")
    artifacts = [normalize_rel(str(item)) for item in config.get("evidenceRepair", {}).get("requiredArtifacts", [])]
    return [normalize_rel("%s/%s/%s" % (root, safe_state_name(work_block_id), artifact)) for artifact in artifacts if artifact]


def git_path_tracked(repo_root: Path, rel_path: str, rev: str = "HEAD") -> bool:
    return run_git(repo_root, ["cat-file", "-e", f"{rev}:{rel_path}"]).returncode == 0


def evidence_missing_or_dirty(repo_root: Path, config: Dict[str, Any], work_block_id: str) -> List[str]:
    paths = evidence_rel_paths(config, work_block_id)
    missing: List[str] = []
    for path in paths:
        status = run_git(repo_root, ["status", "--porcelain=v1", "--", path])
        if status.stdout.strip() or not git_path_tracked(repo_root, path):
            missing.append(path)
    if missing or not paths:
        return missing

    # Evidence is emitted as its own commit and describes that commit's parent.
    # Merely finding tracked JSON is insufficient: a later source commit makes
    # the old evidence stale even though every required path still exists.
    head = git_stdout(repo_root, ["rev-parse", "HEAD"], required=False)
    parent = git_stdout(repo_root, ["rev-parse", "HEAD^"], required=False)
    if not head or not parent:
        return paths
    head_paths = set(
        nul_separated_paths(
            git_stdout(
                repo_root,
                ["diff-tree", "--no-commit-id", "--name-only", "-z", "-r", "HEAD"],
                required=False,
            )
        )
    )
    if head_paths != set(paths):
        return paths
    for path in paths:
        last_commit = git_stdout(repo_root, ["log", "-1", "--format=%H", "--", path], required=False)
        raw = git_stdout(repo_root, ["show", f"HEAD:{path}"], required=False)
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return paths
        if (
            last_commit != head
            or not isinstance(payload, dict)
            or payload.get("workBlockId") != work_block_id
            or payload.get("featureHead") != parent
            or payload.get("artifactKind") != Path(path).stem
        ):
            return paths
    return []


def evidence_payloads(config: Dict[str, Any], detection: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    truth_config = config.get("hardClean", {}).get("unifiedTruthReport", {})
    base = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "workBlockId": detection["workBlockId"],
        "branch": detection["branch"],
        "featureHead": detection["featureHead"],
        "targetHead": detection["targetHead"],
        "detectorHash": detection["detectorHash"],
    }
    if bool(truth_config.get("enabled", True)):
        base["closeoutCleanTruth"] = {
            "status": "pending",
            "authoritativeSource": str(truth_config.get("authoritativeSource") or "repoClosedPostcondition.closeoutCleanTruth"),
            "reason": "evidence_repair_precedes_repo_closed_postcondition",
        }
    return {
        "metrics.json": {
            **base,
            "artifactKind": "metrics",
            "dirtyCounts": {
                "ownedDirty": len(detection.get("ownedDirty", [])),
                "mixedDirty": len(detection.get("mixedDirty", [])),
                "foreignDirty": len(detection.get("foreignDirty", [])),
                "unownedDirty": len(detection.get("unownedDirty", [])),
            },
            "committedDeltaPathCount": len(detection.get("committedDeltaPaths", [])),
        },
        "handoff.json": {
            **base,
            "artifactKind": "handoff",
            "summary": "Generated by brokered closeout evidence repair before publish/final push.",
            "foreignDirtyRetained": [item["path"] for item in detection.get("foreignDirty", [])],
        },
        "session.json": {
            **base,
            "artifactKind": "session",
            "actor": "brokered-closeout",
            "worktree": str(Path.cwd()),
        },
        "closeout.json": {
            **base,
            "artifactKind": "closeout",
            "pinnedRefs": detection.get("pinnedRefs"),
            "eligible": detection.get("eligible"),
        },
    }


def repair_missing_evidence(repo_root_arg: Path, config: Dict[str, Any], detection: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    if not bool(config.get("evidenceRepair", {}).get("enabled", True)):
        return {"status": "disabled", "reason": "evidenceRepair.enabled is false"}
    work_block_id = str(detection["workBlockId"])
    paths = evidence_rel_paths(config, work_block_id)
    missing = evidence_missing_or_dirty(repo_root, config, work_block_id)
    if not missing:
        return {"status": "noop", "reason": "required_evidence_present", "paths": paths}
    claims = active_path_claims(repo_root, config)
    manifest = load_manifest(repo_root, config, work_block_id)
    own_claims = set(manifest_path_claims(manifest))
    blocked: List[Dict[str, Any]] = []
    for path in paths:
        owner = claims.get(path)
        if owner and owner != work_block_id:
            blocked.append({"path": path, "reason": "claimed_by_other_work_block", "ownerWorkBlockId": owner})
        if path_matches_any(path, config.get("paths", {}).get("sensitive", [])):
            blocked.append({"path": path, "reason": "evidence_path_is_sensitive"})
    if blocked:
        payload = {"reason": reason, "blocked": blocked, "paths": paths}
        write_audit(repo_root, config, "evidence_repair_blocked", payload, work_block_id=work_block_id, outcome="blocked")
        return {"status": "blocked", **payload}
    payloads = evidence_payloads(config, detection)
    root = evidence_root(repo_root, config)
    root.mkdir(parents=True, exist_ok=True)
    for rel_path in paths:
        artifact = Path(rel_path).name
        target = safe_repo_path(repo_root, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, payloads.get(artifact, {"schemaVersion": BROKER_SCHEMA_VERSION, "workBlockId": work_block_id, "artifactKind": artifact}))
    updated_claims = sorted(own_claims.union(paths))
    update_manifest(repo_root, config, work_block_id, {"pathClaims": updated_claims})
    add = run_git(repo_root, ["add", "--", *paths])
    if add.returncode != 0:
        result = {"status": "blocked", "reason": "evidence_git_add_failed", "paths": paths, "stderr": add.stderr[-3000:]}
        write_audit(repo_root, config, "evidence_repair_blocked", result, work_block_id=work_block_id, outcome="blocked")
        return result
    commit_message = evidence_repair_commit_message(
        config,
        reason=reason,
        work_block_id=work_block_id,
        paths=paths,
        work_summary=delivered_work_commit_subject(repo_root, detection, manifest),
    )
    commit = run_git(repo_root, ["commit", "-m", commit_message])
    if commit.returncode != 0:
        result = {"status": "blocked", "reason": "evidence_commit_failed", "paths": paths, "stdout": commit.stdout[-3000:], "stderr": commit.stderr[-3000:]}
        write_audit(repo_root, config, "evidence_repair_blocked", result, work_block_id=work_block_id, outcome="blocked")
        return result
    validations: List[Dict[str, Any]] = []
    if bool(config.get("evidenceRepair", {}).get("validateAfterCommit", False)):
        validations = run_validations(repo_root, config, repo_root, changed_paths=paths, work_block_id=work_block_id)
        failed = next((item for item in validations if item["returncode"] != 0), None)
        if failed:
            result = {"status": "blocked", "reason": "evidence_validation_failed", "paths": paths, "validations": validations}
            write_audit(repo_root, config, "evidence_repair_blocked", result, work_block_id=work_block_id, outcome="blocked")
            return result
    result = {"status": "success", "reason": reason, "paths": paths, "commit": git_stdout(repo_root, ["rev-parse", "HEAD"]), "validations": validations}
    write_audit(repo_root, config, "evidence_repair", result, work_block_id=work_block_id, outcome="success")
    append_event(repo_root, config, work_block_id, {"event": "evidence_repaired", "reason": reason, "paths": paths, "commit": result["commit"]})
    return result


def ensure_evidence_for_repair(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any], reason: str, actions: List[Dict[str, Any]], blockers: List[str]) -> Dict[str, Any]:
    if not bool(config.get("evidenceRepair", {}).get("enabled", True)):
        return detection
    required_for = set(config.get("evidenceRepair", {}).get("requiredFor", []))
    if reason not in required_for:
        return detection
    missing = evidence_missing_or_dirty(repo_root, config, str(detection["workBlockId"]))
    if not missing:
        return detection
    evidence = repair_missing_evidence(repo_root, config, detection, reason=reason)
    actions.append({"action": "evidence_repair", **evidence})
    if evidence["status"] != "success":
        blockers.append("evidenceRepairFailed:%s" % reason)
        return detection
    return detect_work_block(repo_root, work_block_id=str(detection["workBlockId"]))


def repair_eligibility(repo_root_arg: Path, *, work_block_id: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="repair", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    tooling = verify_closeout_tooling_current(repo_root, config, plan_only=True)
    if not tooling["ok"]:
        return {"status": "blocked", "reason": "closeout_tooling_stale", "tooling": tooling}
    detection = detect_work_block(repo_root, work_block_id=work_block_id)
    block_id = detection["workBlockId"]
    branch = detection["branch"]
    actions: List[Dict[str, Any]] = []
    blockers: List[str] = []
    triage = dirty_state_remediation_triage(repo_root, config, detection)
    if triage["required"]:
        action_list = [{"actionId": "dirty_state_remediation", "reason": triage["reason"], "triage": triage}]
        freeze_result = enter_remediation_freeze(repo_root, config, reason=str(triage["reason"]), detection=detection, action_list=action_list)
        payload = {"detectionHash": detection["detectorHash"], "actions": action_list, "blockers": [str(triage["reason"])], "triage": triage, "freeze": freeze_result}
        write_json(work_block_dir(repo_root, config, block_id) / "repair.json", payload)
        append_event(repo_root, config, block_id, {"event": "repair_ran", "status": "blocked", "blockers": [str(triage["reason"])]})
        return {"status": "blocked", **payload}
    if detection.get("mixedDirty"):
        paths = [item["path"] for item in detection.get("mixedDirty", [])]
        blockers.append("baseline-dirty-overlaps-candidate:%s" % ",".join(paths))
    if detection["ownedDirty"] and not detection.get("mixedDirty"):
        if bool(config.get("stashPolicy", {}).get("allowOwnedDirtyCheckpoint", True)):
            checkpoint = checkpoint_owned_work(repo_root, work_block_id=block_id)
            actions.append({"action": "checkpoint_owned_dirty", **checkpoint})
            if checkpoint.get("status") == "success":
                detection = detect_work_block(repo_root, work_block_id=block_id)
            if detection["ownedDirty"] and bool(config.get("dirtySplit", {}).get("autoRepairOwnedDirty", True)):
                split = preserve_owned_dirty_split(repo_root, work_block_id=block_id)
                actions.append({"action": "dirty_split", **split})
                detection = detect_work_block(repo_root, work_block_id=block_id)
            if detection["ownedDirty"]:
                blockers.append("ownedDirty_checkpoint_incomplete")
        elif bool(config.get("dirtySplit", {}).get("autoRepairOwnedDirty", True)):
            split = preserve_owned_dirty_split(repo_root, work_block_id=block_id)
            actions.append({"action": "dirty_split", **split})
            detection = detect_work_block(repo_root, work_block_id=block_id)
            if detection["ownedDirty"]:
                blockers.append("ownedDirty_split_incomplete")
        else:
            blockers.append("ownedDirty")
    if detection["unownedDirty"]:
        blockers.append("unownedDirty")
    if detection["foreignDirty"]:
        write_audit(repo_root, config, "retained_foreign_dirty", {"foreignDirty": detection["foreignDirty"]}, work_block_id=block_id, outcome="retained")
    detection = ensure_evidence_for_repair(repo_root, config, detection, "final_push", actions, blockers)
    remote = str(config.get("git", {}).get("remote", "origin"))
    has_remote = remote_exists(repo_root, remote)
    if not has_remote:
        if bool(config.get("git", {}).get("allowLocalOnly", False)):
            actions.append({"action": "no_origin_local_only", "status": "repaired", "reason": "local-only closeout allowed"})
        else:
            blockers.append("missingRemote")
    if has_remote and not is_protected_branch(config, branch):
        upstream = upstream_for(repo_root, branch)
        allowed_remotes = set(config.get("repair", {}).get("allowedPublishRemotes", []))
        if not upstream:
            if bool(config.get("repair", {}).get("autoPublishFeatureBranch", False)) and remote in allowed_remotes:
                detection = ensure_evidence_for_repair(repo_root, config, detection, "publish_missing_upstream", actions, blockers)
                if not any(str(item).startswith("evidenceRepairFailed") for item in blockers):
                    push_args = ["push"]
                    if bool(config.get("repair", {}).get("setUpstreamOnPublish", True)):
                        push_args.append("-u")
                    push_args.extend([remote, f"HEAD:{branch}"])
                    push = run_git(repo_root, push_args)
                    actions.append({"action": "publish_missing_upstream", "remote": remote, "branch": branch, "returncode": push.returncode})
                    if push.returncode != 0:
                        blockers.append("publishMissingUpstreamFailed")
            else:
                blockers.append("missingUpstream")
        else:
            ab = ahead_behind(repo_root, upstream)
            if ab["behind"] > 0:
                blockers.append("featureBehindUpstream")
            elif ab["ahead"] > 0 and bool(config.get("repair", {}).get("autoPublishFeatureBranch", False)):
                detection = ensure_evidence_for_repair(repo_root, config, detection, "publish_ahead_only", actions, blockers)
                if not any(str(item).startswith("evidenceRepairFailed") for item in blockers):
                    ab = ahead_behind(repo_root, upstream)
                    push = run_git(repo_root, ["push", remote, f"HEAD:{branch}"])
                    actions.append({"action": "publish_ahead_only", "remote": remote, "branch": branch, "aheadBehind": ab, "returncode": push.returncode})
                    if push.returncode != 0:
                        blockers.append("publishAheadOnlyFailed")
    status = "repaired" if not blockers else "blocked"
    payload = {"detectionHash": detection["detectorHash"], "actions": actions, "blockers": blockers}
    write_json(work_block_dir(repo_root, config, block_id) / "repair.json", payload)
    append_event(repo_root, config, block_id, {"event": "repair_ran", "status": status, "blockers": blockers})
    if blockers:
        write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
    else:
        write_audit(repo_root, config, "repair", payload, work_block_id=block_id, outcome="success")
    return {"status": status, **payload}


def finalize_candidate_id(work_block_id: str) -> str:
    return "candidate:%s" % work_block_id


def finalize_action_id() -> str:
    return "clean_integrate"


def finalize_evidence(config: Dict[str, Any], detection: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "workBlockId": detection["workBlockId"],
        "detectorHash": detection["detectorHash"],
        "ownedDirty": detection["ownedDirty"],
        "mixedDirty": detection.get("mixedDirty", []),
        "unownedDirty": detection["unownedDirty"],
        "foreignDirty": detection["foreignDirty"],
        "committedDeltaPaths": detection["committedDeltaPaths"],
        "validationCommands": config.get("validation", {}).get("commands", []),
        "pinnedRefs": detection["pinnedRefs"],
    }


def review_tuple(candidate_id: str, action_id: str, evidence_hash: str, policy_hash: str, pinned_refs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "policyHash": policy_hash,
        "pinnedRefs": pinned_refs,
    }


def review_tuple_hash(candidate_id: str, action_id: str, evidence_hash: str, policy_hash: str, pinned_refs: Dict[str, Any]) -> str:
    return stable_hash(review_tuple(candidate_id, action_id, evidence_hash, policy_hash, pinned_refs))


def record_review_approval(
    repo_root_arg: Path,
    *,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
    reviewer: str,
    approved: bool = True,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    tuple_hash = review_tuple_hash(candidate_id, action_id, evidence_hash, str(config.get("policyHash")), pinned_refs)
    safe_reviewer = sha256_text(str(reviewer), 16)
    record = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "reviewer": reviewer,
        "approved": bool(approved),
        "createdAt": utc_now(),
        "tuple": review_tuple(candidate_id, action_id, evidence_hash, str(config.get("policyHash")), pinned_refs),
        "tupleHash": tuple_hash,
        "details": details or {},
    }
    record["reviewHash"] = stable_hash(record)
    write_json(reviews_root(repo_root, config) / tuple_hash / ("%s.json" % safe_reviewer), record)
    write_audit(repo_root, config, "review_recorded", record, outcome="success")
    return record


def review_records(repo_root: Path, config: Dict[str, Any], candidate_id: str, action_id: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    root = reviews_root(repo_root, config)
    if not root.exists():
        return records
    for review_file in root.glob("*/*.json"):
        record = read_json(review_file, {})
        tup = record.get("tuple", {})
        if tup.get("candidateId") == candidate_id and tup.get("actionId") == action_id:
            records.append(record)
    return records


def review_quorum_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    policy = config.get("reviewQuorum", {})
    auto = config.get("autoQuorum", {})
    auto_reviewers = [str(item) for item in auto.get("reviewers", []) if str(item)]
    allowed = {str(item) for item in policy.get("allowedReviewers", []) if str(item)}
    allowed.update(auto_reviewers)
    self_reviewers = [str(item) for item in policy.get("selfReviewers", []) if str(item)] or [
        item for item in auto_reviewers if "self" in item
    ] or ["codex-self"]
    independent_reviewers = [str(item) for item in policy.get("independentReviewers", []) if str(item)] or [
        item for item in auto_reviewers if item not in set(self_reviewers)
    ]
    return {
        "requiredApprovals": max(1, int(policy.get("requiredApprovals", 1))),
        "requiredScore": int(policy.get("requiredScore", auto.get("requiredScore", 10))),
        "allowedReviewers": allowed,
        "selfReviewers": set(self_reviewers),
        "independentReviewers": set(independent_reviewers),
        "requiredSelfApprovals": max(0, int(policy.get("requiredSelfApprovals", 1))),
        "requiredIndependentApprovals": max(0, int(policy.get("requiredIndependentApprovals", 2))),
    }


def declared_review_surfaces(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    policy = config.get("reviewQuorum", {})
    status = str(policy.get("unavailableStatus") or "review_surface_unavailable")
    raw_surfaces = policy.get("declaredSurfaces")
    surfaces: List[Dict[str, Any]] = []
    if isinstance(raw_surfaces, list):
        for item in raw_surfaces:
            if not isinstance(item, dict):
                continue
            surface = str(item.get("surface") or "").strip()
            if not surface:
                continue
            recovery_command = str(
                item.get("recoveryCommand")
                or "Route this tuple to another declared review surface or rerun review-quorum after the surface is available."
            )
            surfaces.append(
                {
                    "surface": surface,
                    "reviewers": [str(reviewer) for reviewer in item.get("reviewers", []) if str(reviewer)],
                    "unavailableStatus": str(item.get("unavailableStatus") or status),
                    "recoveryCommand": recovery_command,
                }
            )
    if surfaces:
        return surfaces
    return [
        {
            "surface": "reviewer:%s" % reviewer,
            "reviewers": [reviewer],
            "unavailableStatus": status,
            "recoveryCommand": "Route this tuple to another declared review surface or rerun review-quorum after the surface is available.",
        }
        for reviewer in sorted(review_quorum_policy(config)["allowedReviewers"])
    ]


def declared_review_surface(config: Dict[str, Any], surface: str) -> Optional[Dict[str, Any]]:
    wanted = str(surface).strip()
    return next((item for item in declared_review_surfaces(config) if item["surface"] == wanted), None)


def safe_review_surface_name(surface: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(surface)).strip("-")
    return safe or sha256_text(str(surface), 12)


def write_review_surface_unavailable_report(
    repo_root_arg: Path,
    *,
    surface: str,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
    unavailable_reason: str = "surface could not perform required review",
    recovery_command: Optional[str] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    surface_policy = declared_review_surface(config, surface)
    if surface_policy is None:
        raise HygieneError("review surface is not declared in reviewQuorum.declaredSurfaces: %s" % surface)
    status = str(surface_policy.get("unavailableStatus") or config.get("reviewQuorum", {}).get("unavailableStatus") or "review_surface_unavailable")
    tuple_hash = review_tuple_hash(candidate_id, action_id, evidence_hash, str(config.get("policyHash")), pinned_refs)
    path = reviews_root(repo_root, config) / "surface-unavailable" / tuple_hash / ("%s.json" % safe_review_surface_name(surface))
    recovery = recovery_command or str(surface_policy.get("recoveryCommand") or "")
    payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "reportType": "review_surface_unavailable",
        "status": status,
        "createdAt": utc_now(),
        "surface": surface_policy["surface"],
        "declaredReviewSurface": True,
        "reviewers": list(surface_policy.get("reviewers", [])),
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "policyHash": config.get("policyHash"),
        "pinnedRefs": pinned_refs,
        "tupleHash": tuple_hash,
        "unavailableReason": unavailable_reason,
        "recoveryCommand": recovery,
        "blockers": [{"kind": status, "surface": surface_policy["surface"], "recoveryCommand": recovery}],
    }
    payload["reportHash"] = stable_hash(payload)
    write_json(path, payload)
    write_audit(repo_root, config, status, payload, outcome="blocked")
    return {
        "status": status,
        "surface": surface_policy["surface"],
        "candidateId": candidate_id,
        "actionId": action_id,
        "tupleHash": tuple_hash,
        "reportHash": payload["reportHash"],
        "reportPath": normalize_rel(str(path.relative_to(repo_root))),
        "recoveryCommand": recovery,
    }


def review_record_score(record: Dict[str, Any]) -> Optional[int]:
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    raw_score = details.get("score")
    try:
        return int(raw_score)
    except (TypeError, ValueError):
        return None


def review_packets_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "review-packets"


def review_packet_dir(repo_root: Path, config: Dict[str, Any], tuple_hash: str) -> Path:
    return review_packets_root(repo_root, config) / tuple_hash


def unblock_detail(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
    action_class: str,
    auto_unblock_allowed: bool,
    blockers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    tuple_hash = review_tuple_hash(candidate_id, action_id, evidence_hash, str(config.get("policyHash")), pinned_refs)
    packet_dir = review_packet_dir(repo_root, config, tuple_hash)
    return {
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "policyHash": config.get("policyHash"),
        "pinnedRefs": pinned_refs,
        "tupleHash": tuple_hash,
        "reviewPacketPath": str(packet_dir / "review-packet.json"),
        "allowedNextCommand": closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config),
        "allowedPhrase": "unblock closeout",
        "actionClass": action_class,
        "autoUnblockAllowed": auto_unblock_allowed,
        "blockers": list(blockers or []),
    }


def write_review_packet(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
    evidence: Dict[str, Any],
    action_class: str,
    auto_unblock_allowed: bool,
    blockers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    detail = unblock_detail(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=evidence_hash,
        pinned_refs=pinned_refs,
        action_class=action_class,
        auto_unblock_allowed=auto_unblock_allowed,
        blockers=blockers,
    )
    packet = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "policyHash": config.get("policyHash"),
        "pinnedRefs": pinned_refs,
        "actionClass": action_class,
        "autoUnblockAllowed": auto_unblock_allowed,
        "blockers": list(blockers or []),
        "evidence": evidence,
        "unblockDetail": detail,
    }
    packet["packetHash"] = stable_hash(packet)
    packet_dir = review_packet_dir(repo_root, config, detail["tupleHash"])
    write_json(packet_dir / "review-packet.json", packet)
    return packet


def auto_quorum_allowed(config: Dict[str, Any], action_class: str, blockers: Optional[Sequence[str]] = None) -> bool:
    auto = config.get("autoQuorum", {})
    if not bool(auto.get("enabled", False)):
        return False
    if blockers:
        return False
    if action_class in set(auto.get("manualOnlyActionClasses", [])):
        return False
    return action_class in set(auto.get("autonomousActionClasses", []))


def autonomous_review_payloads(action_class: str, evidence: Dict[str, Any], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    score = int(config.get("autoQuorum", {}).get("requiredScore", 10))
    reviewers = list(config.get("autoQuorum", {}).get("reviewers", [])) or ["codex-self"]
    rationales = {
        "codex-self": "Exact tuple, policy hash, and symbolic action were generated by the repo actor.",
        "ancestry-safety-reviewer": "The branch head is proven to be an ancestor of the pinned target head.",
        "mutation-scope-reviewer": "The action mutates only the approved local branch ref and retains worktrees/stashes.",
    }
    if action_class == "dirty_split":
        rationales = {
            "codex-self": "The repo actor generated an exact split tuple for owned dirty paths only.",
            "ancestry-safety-reviewer": "The preservation branch is pinned to the feature head before dirty paths are copied.",
            "mutation-scope-reviewer": "The action preserves exact paths before removing those same paths from the original worktree.",
        }
    elif action_class == "owned_dirty_checkpoint":
        rationales = {
            "codex-self": "The repo actor generated an exact checkpoint tuple for owned dirty paths only.",
            "ancestry-safety-reviewer": "The checkpoint is pinned to the current feature and target refs before staging.",
            "mutation-scope-reviewer": "The commit stages only the exact owned dirty paths named in the tuple.",
        }
    payloads: List[Dict[str, Any]] = []
    for reviewer in reviewers:
        payloads.append(
            {
                "reviewer": reviewer,
                "score": score,
                "approved": True,
                "autonomous": True,
                "actionClass": action_class,
                "rationale": rationales.get(reviewer, "Autonomous policy reviewer approved the exact tuple."),
                "evidenceSummary": {
                    "candidate": evidence.get("candidate"),
                    "target": evidence.get("target"),
                },
            }
        )
    return payloads


def ensure_autonomous_quorum(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
    evidence: Dict[str, Any],
    action_class: str,
    blockers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    allowed = auto_quorum_allowed(config, action_class, blockers)
    packet = write_review_packet(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=evidence_hash,
        pinned_refs=pinned_refs,
        evidence=evidence,
        action_class=action_class,
        auto_unblock_allowed=allowed,
        blockers=blockers,
    )
    quorum = check_review_quorum(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=evidence_hash,
        pinned_refs=pinned_refs,
    )
    if quorum["ok"] or not allowed:
        return {"quorum": quorum, "packet": packet, "autoGenerated": False, "unblockDetail": packet["unblockDetail"]}
    reviews: List[Dict[str, Any]] = []
    for payload in autonomous_review_payloads(action_class, evidence, config):
        reviews.append(
            record_review_approval(
                repo_root,
                candidate_id=candidate_id,
                action_id=action_id,
                evidence_hash=evidence_hash,
                pinned_refs=pinned_refs,
                reviewer=str(payload["reviewer"]),
                approved=True,
                details=payload,
            )
        )
    quorum = check_review_quorum(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=evidence_hash,
        pinned_refs=pinned_refs,
    )
    manifest = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "policyHash": config.get("policyHash"),
        "pinnedRefs": pinned_refs,
        "actionClass": action_class,
        "reviewHashes": [review["reviewHash"] for review in reviews],
        "quorum": quorum,
    }
    manifest["manifestHash"] = stable_hash(manifest)
    packet_dir = review_packet_dir(repo_root, config, packet["unblockDetail"]["tupleHash"])
    write_json(packet_dir / "accepted-review-manifest.json", manifest)
    write_audit(repo_root, config, "auto_quorum", {"packet": packet, "manifest": manifest}, outcome="success" if quorum["ok"] else "blocked")
    return {"quorum": quorum, "packet": packet, "autoGenerated": True, "reviews": reviews, "manifest": manifest, "unblockDetail": packet["unblockDetail"]}


def dirty_split_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "dirty-splits"


def dirty_split_worktree_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    configured = str(config.get("dirtySplit", {}).get("worktreeRoot") or ".claude-state/closeout/dirty-splits/worktrees")
    return (repo_root / configured).resolve()


def dirty_split_candidate_id(work_block_id: str, paths: Sequence[str], detector_hash: str) -> str:
    return "candidate:dirty-split:%s" % stable_hash({"workBlockId": work_block_id, "paths": sorted(paths), "detectorHash": detector_hash}, 16)


def dirty_split_branch_name(config: Dict[str, Any], work_block_id: str, candidate_id: str) -> str:
    prefix = normalize_rel(str(config.get("dirtySplit", {}).get("branchPrefix") or "closeout/split")).strip("/")
    return "%s/%s-%s" % (prefix, safe_state_name(work_block_id), stable_hash(candidate_id, 8))


def file_content_hash(path: Path) -> Optional[str]:
    if not path.exists() or path.is_dir():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dirty_path_fingerprints(repo_root: Path, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        path = str(entry["path"])
        rows.append(
            {
                "path": path,
                "status": entry.get("status"),
                "contentSha256": file_content_hash(safe_repo_path(repo_root, path)),
            }
        )
    return rows


def verify_detached_preservation_commit(repo_root: Path, *, branch: str, commit_head: str, entries_by_path: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    branch_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    missing: List[str] = []
    unexpected_present: List[str] = []
    for path, entry in sorted(entries_by_path.items()):
        exists = run_git(repo_root, ["cat-file", "-e", f"{commit_head}:{path}"]).returncode == 0
        if "D" in str(entry.get("status") or ""):
            if exists:
                unexpected_present.append(path)
        elif not exists:
            missing.append(path)
    ok = branch_head == commit_head and not missing and not unexpected_present
    return {
        "ok": ok,
        "branch": branch,
        "expectedHead": commit_head,
        "actualHead": branch_head,
        "missingPaths": missing,
        "unexpectedPresentDeletedPaths": unexpected_present,
    }


def dirty_split_evidence(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any], paths: Sequence[str]) -> Dict[str, Any]:
    path_set = set(paths)
    owned_entries = [item for item in detection["ownedDirty"] if item["path"] in path_set]
    return {
        "workBlockId": detection["workBlockId"],
        "branch": detection["branch"],
        "featureHead": detection["featureHead"],
        "targetHead": detection["targetHead"],
        "detectorHash": detection["detectorHash"],
        "paths": sorted(paths),
        "ownedDirty": owned_entries,
        "dirtyPathFingerprints": dirty_path_fingerprints(repo_root, owned_entries),
        "foreignDirtyCount": len(detection.get("foreignDirty", [])),
        "unownedDirtyCount": len(detection.get("unownedDirty", [])),
        "policy": {
            "branchPrefix": config.get("dirtySplit", {}).get("branchPrefix"),
            "registerBrokerOwnership": config.get("dirtySplit", {}).get("registerBrokerOwnership", True),
            "maxCandidatesPerRun": config.get("dirtySplit", {}).get("maxCandidatesPerRun", 1),
        },
    }


def plan_dirty_split_candidates(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not bool(config.get("dirtySplit", {}).get("enabled", True)):
        return []
    if detection.get("unownedDirty") or detection.get("mixedDirty"):
        return []
    paths = sorted({item["path"] for item in detection.get("ownedDirty", [])})
    if not paths:
        return []
    candidate_id = dirty_split_candidate_id(str(detection["workBlockId"]), paths, str(detection["detectorHash"]))
    branch = dirty_split_branch_name(config, str(detection["workBlockId"]), candidate_id)
    pinned_refs = {
        "feature": detection["pinnedRefs"]["feature"],
        "target": detection["pinnedRefs"]["target"],
        "dirtyPaths": paths,
    }
    evidence = dirty_split_evidence(repo_root, config, detection, paths)
    candidate = {
        "candidateId": candidate_id,
        "actionId": "split",
        "actionClass": "dirty_split",
        "workBlockId": detection["workBlockId"],
        "sourceBranch": detection["branch"],
        "preservationBranch": branch,
        "paths": paths,
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": pinned_refs,
    }
    return [candidate]


def safe_repo_path(repo_root: Path, rel_path: str) -> Path:
    normalized = normalize_rel(rel_path)
    if not normalized or normalized.startswith("../") or normalized == "..":
        raise HygieneError("unsafe repo path: %s" % rel_path)
    resolved = (repo_root / normalized).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise HygieneError("path escapes repo root: %s" % rel_path) from exc
    return resolved


def windows_extended_path(path: Path) -> str:
    resolved = str(path.resolve(strict=False))
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def fs_exists(path: Path) -> bool:
    return os.path.exists(windows_extended_path(path))


def fs_is_dir(path: Path) -> bool:
    return os.path.isdir(windows_extended_path(path))


def fs_remove_path(path: Path) -> None:
    if fs_is_dir(path):
        shutil.rmtree(windows_extended_path(path))
    elif fs_exists(path):
        os.unlink(windows_extended_path(path))


def copy_exact_path_for_split(source_root: Path, dest_root: Path, rel_path: str, status: str) -> Dict[str, Any]:
    source = safe_repo_path(source_root, rel_path)
    dest = safe_repo_path(dest_root, rel_path)
    if "D" in status:
        fs_remove_path(dest)
        return {"path": rel_path, "operation": "delete_in_preservation"}
    if not fs_exists(source):
        raise HygieneError("dirty split source path is missing: %s" % rel_path)
    os.makedirs(windows_extended_path(dest.parent), exist_ok=True)
    if fs_is_dir(source):
        fs_remove_path(dest)
        shutil.copytree(windows_extended_path(source), windows_extended_path(dest))
        return {"path": rel_path, "operation": "copy_directory"}
    shutil.copy2(windows_extended_path(source), windows_extended_path(dest))
    return {"path": rel_path, "operation": "copy_file"}


def remove_exact_path_from_original(repo_root: Path, rel_path: str) -> Dict[str, Any]:
    path = safe_repo_path(repo_root, rel_path)
    tracked = run_git(repo_root, ["ls-files", "--error-unmatch", "--", rel_path])
    if tracked.returncode == 0:
        restore = run_git(repo_root, ["restore", "--staged", "--worktree", "--", rel_path])
        return {"path": rel_path, "operation": "restore_tracked", "returncode": restore.returncode, "stderr": restore.stderr[-2000:]}
    if fs_is_dir(path):
        shutil.rmtree(windows_extended_path(path))
        return {"path": rel_path, "operation": "remove_untracked_directory", "returncode": 0, "stderr": ""}
    if fs_exists(path):
        os.unlink(windows_extended_path(path))
    return {"path": rel_path, "operation": "remove_untracked_file", "returncode": 0, "stderr": ""}


def remove_exact_path_from_worktree(worktree_path: Path, rel_path: str) -> Dict[str, Any]:
    path = safe_repo_path(worktree_path, rel_path)
    tracked = run_git(worktree_path, ["ls-files", "--error-unmatch", "--", rel_path])
    if tracked.returncode == 0:
        restore = run_git(worktree_path, ["restore", "--staged", "--worktree", "--", rel_path])
        return {"path": rel_path, "operation": "restore_tracked", "returncode": restore.returncode, "stderr": restore.stderr[-2000:]}
    if fs_is_dir(path):
        shutil.rmtree(windows_extended_path(path))
        return {"path": rel_path, "operation": "remove_untracked_directory", "returncode": 0, "stderr": ""}
    if fs_exists(path):
        os.unlink(windows_extended_path(path))
    return {"path": rel_path, "operation": "remove_untracked_file", "returncode": 0, "stderr": ""}


def register_dirty_split_manifest(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate: Dict[str, Any],
    worktree_path: Path,
    commit_head: str,
) -> Dict[str, Any]:
    split_id = "split-%s" % stable_hash({"candidateId": candidate["candidateId"], "head": commit_head}, 16)
    manifest = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "workBlockId": split_id,
        "actor": "dirty-split-actor",
        "branch": candidate["preservationBranch"],
        "baseBranch": candidate["sourceBranch"],
        "baseHead": candidate["pinnedRefs"]["feature"]["head"],
        "worktree": str(worktree_path),
        "pathClaims": sorted(candidate["paths"]),
        "state": "completed",
        "createdAt": utc_now(),
        "completedAt": utc_now(),
        "sourceCandidateId": candidate["candidateId"],
    }
    block_dir = work_block_dir(repo_root, config, split_id)
    block_dir.mkdir(parents=True, exist_ok=True)
    write_json(block_dir / "manifest.json", manifest)
    append_event(repo_root, config, split_id, {"event": "dirty_split_preserved", "candidateId": candidate["candidateId"], "paths": candidate["paths"], "head": commit_head})
    return manifest


def apply_dirty_split_candidate(repo_root_arg: Path, candidate: Dict[str, Any]) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    detection = detect_work_block(repo_root, work_block_id=str(candidate["workBlockId"]))
    matching = [
        item
        for item in plan_dirty_split_candidates(repo_root, config, detection)
        if item["candidateId"] == candidate["candidateId"]
    ]
    if not matching:
        payload = {"candidate": candidate, "actualDetectionHash": detection.get("detectorHash"), "detailReason": "candidate_no_longer_current"}
        write_audit(repo_root, config, "dirty_split_stale_tuple", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "stale_tuple", **payload}
    current = matching[0]
    if current["evidenceHash"] != candidate["evidenceHash"] or current["pinnedRefs"] != candidate["pinnedRefs"]:
        payload = {"expected": candidate, "actual": current, "detailReason": "dirty_split_tuple_drifted"}
        write_audit(repo_root, config, "dirty_split_stale_tuple", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "stale_tuple", **payload}
    blockers: List[str] = []
    if detection.get("unownedDirty"):
        blockers.append("unownedDirty")
    if detection.get("mixedDirty"):
        blockers.append("baseline-dirty-overlaps-candidate")
    missing_paths = [path for path in candidate["paths"] if not any(item["path"] == path for item in detection.get("ownedDirty", []))]
    if missing_paths:
        blockers.append("candidate_paths_not_owned_dirty")
    quorum_result = ensure_autonomous_quorum(
        repo_root,
        config,
        candidate_id=candidate["candidateId"],
        action_id=candidate["actionId"],
        evidence_hash=candidate["evidenceHash"],
        pinned_refs=candidate["pinnedRefs"],
        evidence=candidate["evidence"],
        action_class=candidate["actionClass"],
        blockers=blockers,
    )
    if not quorum_result["quorum"]["ok"]:
        payload = {"candidate": candidate, "quorum": quorum_result}
        write_audit(repo_root, config, "review_quorum_blocked", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": quorum_result["quorum"]["reason"] or "review_quorum_blocked", **payload}
    latest_detection = detect_work_block(repo_root, work_block_id=str(candidate["workBlockId"]))
    latest_candidates = [
        item
        for item in plan_dirty_split_candidates(repo_root, config, latest_detection)
        if item["candidateId"] == candidate["candidateId"]
    ]
    if not latest_candidates or latest_candidates[0]["evidenceHash"] != candidate["evidenceHash"] or latest_candidates[0]["pinnedRefs"] != candidate["pinnedRefs"]:
        payload = {"candidate": candidate, "latestDetectionHash": latest_detection.get("detectorHash"), "detailReason": "tuple_drifted_after_quorum"}
        write_audit(repo_root, config, "dirty_split_stale_tuple", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "stale_tuple", **payload}
    worktree_path = dirty_split_worktree_root(repo_root, config) / safe_state_name(candidate["preservationBranch"])
    branch = str(candidate["preservationBranch"])
    existing_branch_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    if existing_branch_head and existing_branch_head != str(candidate["pinnedRefs"]["feature"]["head"]):
        payload = {"candidate": candidate, "branch": branch, "head": existing_branch_head, "reason": "preservation_branch_exists"}
        write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "preservation_branch_exists", **payload}
    if worktree_path.exists():
        payload = {"candidate": candidate, "worktreePath": str(worktree_path), "reason": "preservation_worktree_exists"}
        write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "preservation_worktree_exists", **payload}
    copied: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    add_args = ["worktree", "add", "--no-checkout"]
    if existing_branch_head:
        add_args.extend([str(worktree_path), branch])
    else:
        add_args.extend(["-b", branch, str(worktree_path), str(candidate["pinnedRefs"]["feature"]["head"])])
    add_worktree = run_git_longpaths(repo_root, add_args)
    if add_worktree.returncode != 0:
        payload = {"candidate": candidate, "operation": "worktree_add", "returncode": add_worktree.returncode, "stderr": add_worktree.stderr[-3000:]}
        write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "preservation_worktree_failed", **payload}
    for operation, args in [
        ("sparse_checkout_init", ["sparse-checkout", "init", "--no-cone"]),
        ("sparse_checkout_set", ["sparse-checkout", "set", "--no-cone", *candidate["paths"]]),
        ("sparse_checkout_checkout", ["checkout"]),
    ]:
        result = run_git(worktree_path, args)
        if result.returncode != 0:
            cleanup = remove_worktree(repo_root, worktree_path)
            payload = {
                "candidate": candidate,
                "operation": operation,
                "returncode": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-3000:],
                "cleanup": cleanup,
            }
            write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
            return {"status": "blocked", "reason": "preservation_sparse_checkout_failed", **payload}
    try:
        entries_by_path = {item["path"]: item for item in latest_detection.get("ownedDirty", [])}
        for path in candidate["paths"]:
            copied.append(copy_exact_path_for_split(repo_root, worktree_path, path, str(entries_by_path[path]["status"])))
        add = run_git(worktree_path, ["add", "--", *candidate["paths"]])
        if add.returncode != 0:
            payload = {"candidate": candidate, "operation": "git_add_preservation", "returncode": add.returncode, "stderr": add.stderr[-3000:], "copied": copied}
            write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
            return {"status": "blocked", "reason": "preservation_add_failed", **payload}
        commit = run_git(
            worktree_path,
            ["commit", "-m", dirty_split_commit_message(str(candidate["workBlockId"]), candidate["paths"])],
        )
        if commit.returncode != 0:
            payload = {"candidate": candidate, "operation": "git_commit_preservation", "returncode": commit.returncode, "stdout": commit.stdout[-3000:], "stderr": commit.stderr[-3000:], "copied": copied}
            write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
            return {"status": "blocked", "reason": "preservation_commit_failed", **payload}
        commit_head = git_stdout(worktree_path, ["rev-parse", "HEAD"])
        manifest = None
        if bool(config.get("dirtySplit", {}).get("registerBrokerOwnership", True)):
            manifest = register_dirty_split_manifest(repo_root, config, candidate=candidate, worktree_path=worktree_path, commit_head=commit_head)
        for path in candidate["paths"]:
            removal = remove_exact_path_from_original(repo_root, path)
            removed.append(removal)
            if removal.get("returncode") != 0:
                payload = {"candidate": candidate, "preservationHead": commit_head, "removed": removed, "manifest": manifest}
                write_audit(repo_root, config, "dirty_split_partial_recovery", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
                return {"status": "blocked", "reason": "original_cleanup_failed", **payload}
        payload = {
            "candidate": candidate,
            "preservationBranch": branch,
            "preservationWorktree": str(worktree_path),
            "preservationHead": commit_head,
            "copied": copied,
            "removedFromOriginal": removed,
            "manifest": manifest,
        }
        write_audit(repo_root, config, "dirty_split_success", payload, work_block_id=str(candidate["workBlockId"]), outcome="success")
        return {"status": "success", **payload}
    except Exception as exc:
        payload = {"candidate": candidate, "copied": copied, "removed": removed, "error": str(exc)}
        write_audit(repo_root, config, "dirty_split_partial_recovery", payload, work_block_id=str(candidate["workBlockId"]), outcome="blocked")
        return {"status": "blocked", "reason": "dirty_split_exception", **payload}


def preserve_owned_dirty_split(repo_root_arg: Path, *, work_block_id: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="dirty-split", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    detection = detect_work_block(repo_root, work_block_id=work_block_id)
    candidates = plan_dirty_split_candidates(repo_root, config, detection)
    limit = max(1, int(config.get("dirtySplit", {}).get("maxCandidatesPerRun", 1)))
    candidates = candidates[:limit]
    if not candidates:
        if detection.get("mixedDirty"):
            payload = {"reason": "baseline-dirty-overlaps-candidate", "mixedDirty": detection["mixedDirty"]}
            write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=detection["workBlockId"], outcome="blocked")
            return {"status": "blocked", **payload}
        if detection.get("unownedDirty"):
            payload = {"reason": "unowned_dirty_requires_triage", "unownedDirty": detection["unownedDirty"]}
            write_audit(repo_root, config, "dirty_split_blocked_preservation", payload, work_block_id=detection["workBlockId"], outcome="blocked")
            return {"status": "blocked", **payload}
        return {"status": "noop", "reason": "no_owned_dirty_split_candidate"}
    results = [apply_dirty_split_candidate(repo_root, candidate) for candidate in candidates]
    status = "success" if all(item["status"] == "success" for item in results) else "blocked"
    return {"status": status, "candidates": candidates, "results": results}


def check_review_quorum(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate_id: str,
    action_id: str,
    evidence_hash: str,
    pinned_refs: Dict[str, Any],
) -> Dict[str, Any]:
    high_impact = set(config.get("reviewQuorum", {}).get("highImpactActions", HIGH_IMPACT_ACTIONS))
    policy = review_quorum_policy(config)
    required = int(policy["requiredApprovals"])
    tuple_hash = review_tuple_hash(candidate_id, action_id, evidence_hash, str(config.get("policyHash")), pinned_refs)
    records = review_records(repo_root, config, candidate_id, action_id)
    matching_raw = [record for record in records if record.get("tupleHash") == tuple_hash and record.get("approved") is True]
    stale = [record for record in records if record.get("tupleHash") != tuple_hash]
    authorized: List[Dict[str, Any]] = []
    unauthorized: List[Dict[str, Any]] = []
    low_score: List[Dict[str, Any]] = []
    self_reviewers = set(policy["selfReviewers"])
    independent_reviewers = set(policy["independentReviewers"])
    allowed_reviewers = set(policy["allowedReviewers"])
    required_score = int(policy["requiredScore"])
    for record in matching_raw:
        reviewer = str(record.get("reviewer") or "")
        if allowed_reviewers and reviewer not in allowed_reviewers:
            unauthorized.append(record)
            continue
        score = review_record_score(record)
        if score is None or score < required_score:
            low_score.append(record)
            continue
        authorized.append(record)
    authorized_reviewers = {str(record.get("reviewer") or "") for record in authorized}
    self_count = len(authorized_reviewers & self_reviewers)
    independent_count = len(authorized_reviewers & independent_reviewers)
    high_impact_ok = (
        len(authorized_reviewers) >= required
        and self_count >= int(policy["requiredSelfApprovals"])
        and independent_count >= int(policy["requiredIndependentApprovals"])
    )
    ok = action_id not in high_impact or high_impact_ok
    reason = None
    if not ok:
        reason = "stale_review" if stale else "review_quorum_missing"
    return {
        "ok": ok,
        "requiredApprovals": required,
        "matchingApprovals": len(authorized_reviewers),
        "rawMatchingApprovals": len(matching_raw),
        "unauthorizedApprovalCount": len(unauthorized),
        "lowScoreApprovalCount": len(low_score),
        "requiredScore": required_score,
        "selfReviewers": sorted(self_reviewers),
        "independentReviewers": sorted(independent_reviewers),
        "selfApprovalCount": self_count,
        "independentApprovalCount": independent_count,
        "tupleHash": tuple_hash,
        "staleReviewCount": len(stale),
        "reason": reason,
        "insufficientReviewStatus": None if ok else str(config.get("reviewQuorum", {}).get("insufficientReviewStatus") or "insufficient_review_quorum"),
        "declaredReviewSurfaces": [item["surface"] for item in declared_review_surfaces(config)],
    }


def validation_command_timeout_ms(config: Dict[str, Any], command: Dict[str, Any]) -> int:
    validation = config.get("validation", {})
    return positive_int(command.get("timeoutMs"), positive_int(validation.get("timeoutMs"), closeout_command_timeout_ms(config, ["finalize"])))


def validation_command_max_output_bytes(config: Dict[str, Any], command: Dict[str, Any]) -> int:
    validation = config.get("validation", {})
    return positive_int(command.get("maxOutputBytes"), positive_int(validation.get("maxOutputBytes"), closeout_max_process_output_bytes(config)))


def validation_full_suite_requested(config: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> bool:
    validation = config.get("validation", {})
    if bool(validation.get("runFullSuiteByDefault", False)):
        return True
    env_var = str(validation.get("fullSuiteEnvVar") or "CLOSEOUT_RUN_FULL_VALIDATION")
    value = (env or os.environ).get(env_var, "")
    return str(value).strip().lower() in {"1", "true", "yes", "on", "full"}


def validation_command_skip_reason(
    command: Dict[str, Any],
    changed_paths: Optional[Sequence[str]],
    *,
    include_full: bool = True,
) -> Optional[str]:
    run_mode = str(command.get("runMode") or "smoke").strip().lower()
    if run_mode == "full" and not include_full:
        return "full_validation_not_requested"
    patterns = command.get("pathPatterns")
    if not isinstance(patterns, list) or not patterns:
        return None
    if changed_paths is None:
        return None
    normalized_paths = [normalize_rel(str(path)) for path in changed_paths if normalize_rel(str(path))]
    if not normalized_paths:
        return "path_patterns_not_matched"
    if not any(path_matches_any(path, patterns) for path in normalized_paths):
        return "path_patterns_not_matched"
    return None


def validation_command_applies(command: Dict[str, Any], changed_paths: Optional[Sequence[str]], *, include_full: bool = True) -> bool:
    return validation_command_skip_reason(command, changed_paths, include_full=include_full) is None


def run_validations(
    repo_root: Path,
    config: Dict[str, Any],
    integration_path: Path,
    *,
    changed_paths: Optional[Sequence[str]] = None,
    work_block_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    include_full = validation_full_suite_requested(config)
    for command in config.get("validation", {}).get("commands", []):
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise HygieneError("validation command argv must be a non-empty string array")
        argv = list(argv)
        if argv[0] == "python":
            argv[0] = sys.executable
        name = str(command.get("name") or argv[0])
        skip_reason = validation_command_skip_reason(command, changed_paths, include_full=include_full)
        if skip_reason:
            results.append(
                {
                    "name": name,
                    "argv": argv,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "skipped": True,
                    "skipReason": skip_reason,
                    "runMode": str(command.get("runMode") or "smoke"),
                    "pathPatterns": list(command.get("pathPatterns") or []),
                    "changedPaths": sorted({normalize_rel(str(path)) for path in changed_paths or [] if normalize_rel(str(path))}),
                }
            )
            continue
        completed = run_bounded_closeout_process(
            repo_root,
            config,
            argv,
            timeout_ms=validation_command_timeout_ms(config, command),
            max_output_bytes=validation_command_max_output_bytes(config, command),
            recovery_command="rerun validation command %s" % name,
            closeout_args=["validation", name],
            work_block_id=work_block_id,
            normalize_failure_text=False,
            cwd=integration_path,
            resource_overrides=command,
        )
        results.append(
            {
                "name": name,
                "argv": argv,
                "returncode": int(completed["returncode"]),
                "stdout": str(completed.get("stdout") or "")[-4000:],
                "stderr": str(completed.get("stderr") or "")[-4000:],
                "status": completed.get("status"),
                "timedOut": bool(completed.get("timedOut")),
                "outputCapped": bool(completed.get("outputCapped")),
                "cpuStalled": bool(completed.get("cpuStalled")),
                "cpuWatchdog": completed.get("cpuWatchdog"),
                "resourcePolicy": completed.get("resourcePolicy"),
                "resourceApply": completed.get("resourceApply"),
                "killedProcessTree": bool(completed.get("killedProcessTree")),
                "killedDescendants": completed.get("killedDescendants", []),
                "timeoutMs": completed.get("timeoutMs"),
                "maxOutputBytes": completed.get("maxOutputBytes"),
            }
        )
        if int(completed["returncode"]) != 0:
            break
    return results


def tree_hash(repo_root: Path, rev: str) -> str:
    return git_stdout(repo_root, ["rev-parse", f"{rev}^{{tree}}"])


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return run_git(repo_root, ["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def stale_locked_worktree_admin_entries(repo_root: Path) -> List[Dict[str, Any]]:
    """Admin entries that `git worktree prune` can never clear on its own.

    `git worktree add` locks a new worktree with reason "initializing" and unlocks it once
    setup completes. When that process is killed midway -- which the bounded closeout runner
    does deliberately on timeout or CPU stall -- the lock survives with no directory behind
    it. `git worktree prune` skips locked worktrees BY DESIGN, so the stale registry entry is
    structurally unprunable and keeps blocking closeout forever.

    Only entries whose worktree directory is ALREADY GONE are reported, so unlocking them
    cannot destroy anything that exists.
    """
    admin_root = repo_root / ".git" / "worktrees"
    entries: List[Dict[str, Any]] = []
    if not admin_root.is_dir():
        return entries
    for admin_dir in sorted(admin_root.iterdir()):
        lock_file = admin_dir / "locked"
        gitdir_file = admin_dir / "gitdir"
        if not lock_file.is_file() or not gitdir_file.is_file():
            continue
        try:
            gitdir_value = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not gitdir_value:
            continue
        worktree_path = Path(gitdir_value).parent
        if worktree_path.exists():
            continue
        try:
            reason = lock_file.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        entries.append({"admin": admin_dir.name, "worktreePath": str(worktree_path), "lockReason": reason})
    return entries


def prune_worktrees(repo_root: Path) -> Dict[str, Any]:
    """Prune, then clear stale LOCKED entries that prune structurally cannot, then prune again.

    Unlocking is confined to entries whose directory is already gone -- see
    stale_locked_worktree_admin_entries -- so no live or locked-for-a-reason worktree is touched.
    """
    prune = run_git_longpaths(repo_root, ["worktree", "prune"])
    stale = stale_locked_worktree_admin_entries(repo_root)
    unlocked: List[Dict[str, Any]] = []
    for entry in stale:
        unlock = run_git_longpaths(repo_root, ["worktree", "unlock", entry["worktreePath"]])
        unlocked.append({**entry, "unlockReturncode": unlock.returncode, "unlockStderr": unlock.stderr[-500:]})
    if unlocked:
        prune = run_git_longpaths(repo_root, ["worktree", "prune"])
    return {
        "pruneReturncode": prune.returncode,
        "pruneStderr": prune.stderr[-2000:],
        "staleLockedUnlocked": unlocked,
    }


def remove_worktree(repo_root: Path, path: Path) -> Dict[str, Any]:
    result = run_git_longpaths(repo_root, ["worktree", "remove", "--force", "--force", str(path)])
    if result.returncode != 0 and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    prune = prune_worktrees(repo_root)
    return {"path": str(path), "returncode": result.returncode, "stderr": result.stderr[-2000:], **prune}


def target_branch_worktrees(repo_root: Path, target_branch: str) -> List[Dict[str, Any]]:
    target_ref = f"refs/heads/{target_branch}"
    return [item for item in parse_worktree_list(repo_root) if item.get("branchRef") == target_ref]


def target_worktree_update_preflight(repo_root: Path, target_branch: str, new_head: str) -> Dict[str, Any]:
    local_ref = f"refs/heads/{target_branch}"
    local_head = rev_parse(repo_root, local_ref, required=False)
    checked_out = target_branch_worktrees(repo_root, target_branch)
    payload: Dict[str, Any] = {
        "status": "success",
        "targetBranch": target_branch,
        "localHead": local_head,
        "newHead": new_head,
        "checkedOutWorktrees": [str(item.get("path") or "") for item in checked_out],
    }
    if len(checked_out) > 1:
        return {
            **payload,
            "status": "blocked",
            "reason": "target_branch_checked_out_multiple_worktrees",
        }
    if not checked_out:
        return {**payload, "method": "update-ref"}
    worktree_path = Path(str(checked_out[0]["path"]))
    dirty = worktree_dirty_state(worktree_path)
    payload["worktree"] = str(worktree_path)
    payload["dirtyState"] = dirty
    if dirty.get("dirty"):
        return {
            **payload,
            "status": "blocked",
            "reason": "target_worktree_dirty_before_ref_update",
        }
    if local_head and local_head != new_head and not is_ancestor(repo_root, local_head, new_head):
        return {
            **payload,
            "status": "blocked",
            "reason": "target_worktree_not_fast_forwardable",
        }
    return {**payload, "method": "checked-out-worktree-ff"}


def update_local_target(repo_root: Path, target_branch: str, new_head: str) -> Dict[str, Any]:
    preflight = target_worktree_update_preflight(repo_root, target_branch, new_head)
    result_payload: Dict[str, Any] = {
        "targetBranch": target_branch,
        "newHead": new_head,
        "preflight": preflight,
        "method": preflight.get("method"),
    }
    if preflight["status"] != "success":
        return {
            **result_payload,
            "returncode": 1,
            "reason": preflight.get("reason"),
            "stderr": preflight.get("reason", "target worktree preflight failed"),
        }
    if preflight.get("method") == "update-ref":
        result = run_git(repo_root, ["update-ref", f"refs/heads/{target_branch}", new_head])
        return {
            **result_payload,
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        }

    worktree_path = Path(str(preflight["worktree"]))
    local_head = str(preflight.get("localHead") or "")
    if local_head == new_head:
        result_returncode = 0
        stdout = "target ref already points at requested head"
        stderr = ""
    else:
        result = run_git(worktree_path, ["merge", "--ff-only", new_head])
        result_returncode = result.returncode
        stdout = result.stdout[-2000:]
        stderr = result.stderr[-2000:]
    head_after = rev_parse(repo_root, f"refs/heads/{target_branch}", required=False)
    dirty_after = worktree_dirty_state(worktree_path)
    verified = result_returncode == 0 and head_after == new_head and not dirty_after.get("dirty")
    return {
        **result_payload,
        "returncode": 0 if verified else (result_returncode or 1),
        "stdout": stdout,
        "stderr": stderr if result_returncode else ("" if verified else "target worktree did not synchronize cleanly"),
        "headAfter": head_after,
        "dirtyStateAfter": dirty_after,
        "verified": verified,
    }


def push_failed_non_fast_forward(push_result: Dict[str, Any]) -> bool:
    text = "%s\n%s" % (push_result.get("stdout") or "", push_result.get("stderr") or "")
    lowered = text.lower()
    return "non-fast-forward" in lowered or "updates were rejected" in lowered or "fetch first" in lowered


def target_push_recovery_command(remote: str, target_branch: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "fetch": "git fetch %s %s" % (remote, target_branch),
        "integrateLocalTarget": "switch to %s, then run git merge --ff-only %s/%s" % (target_branch, remote, target_branch),
        "rerunCloseout": closeout_script_command("work-block-complete.ps1", ["-Finalize"], config),
        "raceAdvice": "If another closeout keeps moving %s/%s, wait for it to finish, fetch again, then rerun closeout." % (remote, target_branch),
    }


def repair_target_push_failure(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    target_branch: str,
    remote: str,
    attempted_head: str,
    push_result: Dict[str, Any],
    work_block_id: Optional[str] = None,
) -> Dict[str, Any]:
    command = target_push_recovery_command(remote, target_branch, config)
    recovery: Dict[str, Any] = {
        "status": "blocked",
        "reason": "target_push_failed",
        "recoveryCommand": command,
        "safeRecovery": command,
        "push": push_result,
        "remote": remote,
        "targetBranch": target_branch,
        "attemptedHead": attempted_head,
    }
    if not push_failed_non_fast_forward(push_result):
        write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
        return recovery
    fetch = run_git(repo_root, ["fetch", "--prune", remote, target_branch])
    remote_ref = f"refs/remotes/{remote}/{target_branch}"
    local_ref = f"refs/heads/{target_branch}"
    remote_head = rev_parse(repo_root, remote_ref, required=False)
    local_head = rev_parse(repo_root, local_ref, required=False)
    recovery.update(
        {
            "reason": "target_push_non_fast_forward",
            "fetch": {"returncode": fetch.returncode, "stdout": fetch.stdout[-2000:], "stderr": fetch.stderr[-2000:]},
            "remoteHeadAfterFetch": remote_head,
            "localHeadBeforeUpdate": local_head,
        }
    )
    if fetch.returncode != 0:
        recovery["reason"] = "target_push_recovery_fetch_failed"
        write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
        return recovery
    if not remote_head:
        recovery["reason"] = "target_push_remote_ref_missing_after_fetch"
        write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
        return recovery
    if local_head != remote_head and (not local_head or is_ancestor(repo_root, local_head, remote_head)):
        local_update = update_local_target(repo_root, target_branch, remote_head)
        recovery["localTargetUpdate"] = local_update
        recovery["localHeadAfterUpdate"] = rev_parse(repo_root, local_ref, required=False)
        if local_update["returncode"] != 0:
            recovery["reason"] = "target_push_local_update_failed"
            write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
            return recovery
    elif local_head != remote_head:
        recovery["reason"] = "target_push_local_target_not_fast_forwardable"
        write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
        return recovery
    else:
        recovery["localHeadAfterUpdate"] = local_head
    if remote_head == attempted_head or is_ancestor(repo_root, attempted_head, remote_head):
        recovery.update({"status": "success", "reason": "target_push_remote_already_contains_attempted_head", "targetHeadAfter": remote_head})
        write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="success")
        return recovery
    recovery.update(
        {
            "status": "blocked",
            "reason": "target_push_rerun_required",
            "targetHeadAfter": remote_head,
        }
    )
    write_audit(repo_root, config, "target_push_recovery", recovery, work_block_id=work_block_id, outcome="blocked")
    return recovery


def cleanup_after_success(
    repo_root: Path,
    config: Dict[str, Any],
    detection: Dict[str, Any],
    *,
    new_target_head: str,
    integration_path: Optional[Path],
) -> Dict[str, Any]:
    branch = detection["branch"]
    target_branch = detection["targetBranch"]
    cleanup: Dict[str, Any] = {"actions": [], "retained": []}
    tree_equal = tree_hash(repo_root, detection["featureHead"]) == tree_hash(repo_root, new_target_head)
    upstream = upstream_for(repo_root, branch)
    if integration_path is not None:
        cleanup["actions"].append({"action": "integration_worktree_remove", **remove_worktree(repo_root, integration_path)})
        write_audit(repo_root, config, "snapshot_pruning", cleanup["actions"][-1], work_block_id=detection["workBlockId"], outcome="success")
    delete_remote_after_success = bool(config.get("cleanupPolicy", {}).get("deleteRemoteBranchAfterSuccess", False)) or bool(config.get("hardClean", {}).get("requireForCompletion", False))
    if delete_remote_after_success and upstream and "/" in upstream and branch != target_branch:
        remote, remote_branch = upstream.split("/", 1)
        remote_cleanup = delete_remote_feature_ref(repo_root, config, remote=remote, branch=remote_branch, expected_head=str(detection["featureHead"]))
        cleanup["actions"].append(remote_cleanup)
        if remote_cleanup.get("status") != "success":
            cleanup["retained"].append({"reason": remote_cleanup.get("reason") or "remote_branch_delete_failed", "branch": branch, "upstream": upstream})
    if bool(config.get("cleanupPolicy", {}).get("deleteLocalBranchAfterSuccess", True)) and branch != target_branch:
        if current_branch(repo_root) == branch:
            if tree_equal:
                switch = run_git(repo_root, ["switch", target_branch])
                cleanup["actions"].append({"action": "switch_original_worktree_to_target", "returncode": switch.returncode, "stderr": switch.stderr[-2000:]})
                if switch.returncode != 0:
                    cleanup["retained"].append({"reason": "switch_to_target_failed", "branch": branch})
                    write_audit(repo_root, config, "cleanup_retention", cleanup, work_block_id=detection["workBlockId"], outcome="retained")
                    return cleanup
            else:
                cleanup["retained"].append({"reason": "target_tree_does_not_match_feature_tree", "branch": branch})
                write_audit(repo_root, config, "cleanup_retention", cleanup, work_block_id=detection["workBlockId"], outcome="retained")
                return cleanup
        delete = run_git(repo_root, ["branch", "-d", branch])
        cleanup["actions"].append({"action": "delete_local_branch", "branch": branch, "returncode": delete.returncode, "stderr": delete.stderr[-2000:]})
        if delete.returncode == 0:
            write_audit(repo_root, config, "branch_deletion", cleanup["actions"][-1], work_block_id=detection["workBlockId"], outcome="success")
        else:
            cleanup["retained"].append({"reason": "local_branch_delete_failed", "branch": branch})
            write_audit(repo_root, config, "cleanup_retention", cleanup, work_block_id=detection["workBlockId"], outcome="retained")
    return cleanup


def runtime_command_argv(command: Any) -> List[str]:
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        return []
    argv = list(command)
    if argv[0] == "python":
        argv[0] = sys.executable
    return argv


def enabled_runtime_services(config: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    services = config.get("runtimeServices", {})
    if not isinstance(services, dict):
        return []
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for name, service in sorted(services.items()):
        if isinstance(service, dict) and bool(service.get("enabled", False)):
            rows.append((str(name), service))
    return rows


def runtime_service_recovery_command(service_name: str, command_name: str) -> str:
    return "fix runtimeServices.%s.%s or stop/restart the service manually, then rerun work-block-complete -Finalize -RequireRepoClosed" % (service_name, command_name)


def run_runtime_service_command(repo_root: Path, config: Dict[str, Any], service_name: str, service: Dict[str, Any], command_name: str) -> Dict[str, Any]:
    argv = runtime_command_argv(service.get(command_name))
    if not argv:
        return {
            "service": service_name,
            "command": command_name,
            "configured": False,
            "returncode": None,
            "stdout": "",
            "stderr": "runtime service command is not configured",
        }
    completed = run_bounded_closeout_process(
        repo_root,
        config,
        argv,
        timeout_ms=positive_int(service.get("timeoutMs"), closeout_command_timeout_ms(config, ["repair"])),
        max_output_bytes=closeout_max_process_output_bytes(config),
        recovery_command=runtime_service_recovery_command(service_name, command_name),
        closeout_args=["runtime-service"],
    )
    return {
        "service": service_name,
        "command": command_name,
        "configured": True,
        "argv": argv,
        "returncode": completed["returncode"],
        "status": completed["status"],
        "stdout": completed["stdout"][-2000:],
        "stderr": completed["stderr"][-2000:],
        "timedOut": completed["timedOut"],
        "outputCapped": completed["outputCapped"],
    }


def stop_runtime_services_before_promotion(repo_root: Path, config: Dict[str, Any], *, work_block_id: Optional[str] = None) -> Dict[str, Any]:
    services: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    for service_name, service in enabled_runtime_services(config):
        if not bool(service.get("stopBeforePromotion", False)):
            continue
        row: Dict[str, Any] = {"service": service_name, "phase": "stopBeforePromotion"}
        status_before = run_runtime_service_command(repo_root, config, service_name, service, "statusCommand")
        row["statusBefore"] = status_before
        if not status_before["configured"]:
            row["status"] = "blocked"
            row["reason"] = "runtime_service_status_command_missing"
            row["recoveryCommand"] = runtime_service_recovery_command(service_name, "statusCommand")
            blockers.append(row)
            services.append(row)
            continue
        if status_before["returncode"] != 0:
            row["status"] = "success"
            row["alreadyStopped"] = True
            row["stoppedByCloseout"] = False
            services.append(row)
            continue
        stop = run_runtime_service_command(repo_root, config, service_name, service, "stopCommand")
        row["stop"] = stop
        if not stop["configured"] or stop["returncode"] != 0:
            row["status"] = "blocked"
            row["reason"] = "runtime_service_stop_failed" if stop["configured"] else "runtime_service_stop_command_missing"
            row["recoveryCommand"] = runtime_service_recovery_command(service_name, "stopCommand")
            blockers.append(row)
            services.append(row)
            continue
        status_after = run_runtime_service_command(repo_root, config, service_name, service, "statusCommand")
        row["statusAfter"] = status_after
        if status_after["returncode"] == 0:
            row["status"] = "blocked"
            row["reason"] = "runtime_service_still_running_after_stop"
            row["recoveryCommand"] = runtime_service_recovery_command(service_name, "stopCommand")
            blockers.append(row)
            services.append(row)
            continue
        row["status"] = "success"
        row["alreadyStopped"] = False
        row["stoppedByCloseout"] = True
        services.append(row)
    result = {
        "status": "blocked" if blockers else "success",
        "reason": "runtime_service_stop_failed" if blockers else None,
        "phase": "stopBeforePromotion",
        "services": services,
        "blockers": blockers,
    }
    if services or blockers:
        write_audit(repo_root, config, "runtime_service_stop", result, work_block_id=work_block_id, outcome="blocked" if blockers else "success")
    return result


def restart_runtime_services_after_clean_promotion(
    repo_root: Path,
    config: Dict[str, Any],
    stop_result: Optional[Dict[str, Any]],
    *,
    work_block_id: Optional[str] = None,
) -> Dict[str, Any]:
    stopped_by_closeout = {
        str(item.get("service"))
        for item in (stop_result or {}).get("services", [])
        if item.get("stoppedByCloseout")
    }
    services: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    for service_name, service in enabled_runtime_services(config):
        if not bool(service.get("restartAfterCleanPromotion", False)):
            continue
        row: Dict[str, Any] = {"service": service_name, "phase": "restartAfterCleanPromotion"}
        if service_name not in stopped_by_closeout:
            row["status"] = "skipped"
            row["reason"] = "service_was_not_stopped_by_closeout"
            services.append(row)
            continue
        start = run_runtime_service_command(repo_root, config, service_name, service, "startCommand")
        row["start"] = start
        if not start["configured"] or start["returncode"] != 0:
            row["status"] = "blocked"
            row["reason"] = "runtime_service_restart_failed" if start["configured"] else "runtime_service_start_command_missing"
            row["recoveryCommand"] = runtime_service_recovery_command(service_name, "startCommand")
            blockers.append(row)
            services.append(row)
            continue
        status_after = run_runtime_service_command(repo_root, config, service_name, service, "statusCommand")
        row["statusAfter"] = status_after
        if not status_after["configured"] or status_after["returncode"] != 0:
            row["status"] = "blocked"
            row["reason"] = "runtime_service_not_running_after_restart"
            row["recoveryCommand"] = runtime_service_recovery_command(service_name, "statusCommand")
            blockers.append(row)
            services.append(row)
            continue
        row["status"] = "success"
        services.append(row)
    result = {
        "status": "blocked" if blockers else "success",
        "reason": "runtime_service_restart_failed" if blockers else None,
        "phase": "restartAfterCleanPromotion",
        "services": services,
        "blockers": blockers,
    }
    if services or blockers:
        write_audit(repo_root, config, "runtime_service_restart", result, work_block_id=work_block_id, outcome="blocked" if blockers else "success")
    return result


def audit_type_exists(repo_root: Path, config: Dict[str, Any], audit_types: Sequence[str], *, work_block_id: Optional[str] = None) -> bool:
    path = audit_root(repo_root, config) / "audits.jsonl"
    if not path.exists():
        return False
    wanted = set(audit_types)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("auditType") not in wanted:
            continue
        if work_block_id is not None and str(row.get("workBlockId")) != str(work_block_id):
            continue
        return True
    return False


def repo_relative_or_absolute(repo_root: Path, path: Path) -> str:
    try:
        return normalize_rel(str(path.resolve().relative_to(repo_root.resolve())))
    except ValueError:
        return normalize_rel(str(path))


def hard_clean_stale_transaction_branches(repo_root: Path, config: Dict[str, Any], target_branch: str) -> List[Dict[str, Any]]:
    stale: List[Dict[str, Any]] = []
    for row in local_branch_rows(repo_root):
        branch = str(row["branch"])
        if branch == target_branch or is_protected_branch(config, branch):
            continue
        if branch_allowed_by_policy(config, branch) or branch.startswith("closeout/"):
            stale.append(row)
    return stale


def hard_clean_stale_worktrees(repo_root: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    patterns = list(config.get("responseHookLifecycle", {}).get("managedSessionWorktreeRoots", []))
    state_root = normalize_rel(str(config.get("stateRoot") or ".claude-state/closeout")).strip("/")
    patterns.extend(
        [
            f"{state_root}/integration-worktrees/**",
            f"{state_root}/rs-iw/**",
            f"{state_root}/runtime-services/**",
            normalize_rel(str(config.get("dirtySplit", {}).get("worktreeRoot") or ".claude-state/closeout/dirty-splits/worktrees")).strip("/") + "/**",
        ]
    )
    stale: List[Dict[str, Any]] = []
    repo_resolved = repo_root.resolve()
    primary_root = primary_worktree_root(repo_root)
    for item in worktree_rows(repo_root):
        path = Path(str(item.get("path") or ""))
        if not path:
            continue
        try:
            resolved = path.resolve()
            if resolved == repo_resolved or (primary_root is not None and resolved == primary_root):
                continue
        except OSError:
            pass
        rel = repo_relative_or_absolute(repo_root, path)
        managed = path_matches_any(rel, patterns) or path_matches_any(str(path), patterns)
        detached = bool(item.get("detached") or not item.get("branch"))
        if detached or managed:
            stale.append({**item, "repoRelativePath": rel, "managed": managed, "detached": detached})
    return stale


def orphaned_closeout_runtime_artifacts(repo_root: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    root = closeout_state_root(repo_root, config)
    candidates = [
        root / "integration-worktrees",
        root / "rs-iw",
        root / "runtime-services",
    ]
    artifacts: List[Dict[str, Any]] = []
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_dir():
            continue
        children = sorted(candidate.iterdir(), key=lambda item: item.name)
        if children:
            artifacts.append(
                {
                    "path": repo_relative_or_absolute(repo_root, candidate),
                    "children": [repo_relative_or_absolute(repo_root, child) for child in children[:50]],
                    "childCount": len(children),
                }
            )
    return artifacts


def agent_remediation_repo_closed_state(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    packets = pending_agent_remediation_queue_packets(repo_root, config)
    remote_failures: List[Dict[str, Any]] = []
    if packets:
        remote_failures = refresh_agent_queue_remote_refs(repo_root, config, packets)
    blockers: List[Dict[str, Any]] = []
    packet_rows: List[Dict[str, Any]] = []
    for packet in packets:
        stale_reasons = [*remote_failures, *agent_remediation_packet_stale_reasons(repo_root, config, packet)]
        row = {
            "candidateId": packet.get("candidateId"),
            "workBlockId": packet.get("workBlockId"),
            "actionId": packet.get("actionId"),
            "queuePath": packet.get("queuePath"),
            "staleReasons": stale_reasons,
            "shardCount": len(packet.get("shards", [])) if isinstance(packet.get("shards"), list) else 0,
        }
        packet_rows.append(row)
        if stale_reasons:
            blockers.append({"kind": "stale_agent_remediation_queue_packet", **row})
            continue
        for shard in packet.get("shards", []) if isinstance(packet.get("shards"), list) else []:
            try:
                result_path = resolve_agent_result_path(repo_root, config, str(shard.get("resultPath") or ""))
            except HygieneError as exc:
                blockers.append({"kind": "agent_result_path_invalid", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "error": str(exc)})
                continue
            if not result_path.exists():
                blockers.append({"kind": "missing_agent_remediation_result", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "resultPath": str(result_path)})
                continue
            try:
                payload = read_json(result_path, {})
            except Exception as exc:
                blockers.append({"kind": "unreadable_agent_remediation_result", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "resultPath": str(result_path), "error": str(exc)})
                continue
            schema = shard.get("expectedOutputSchema") if isinstance(shard.get("expectedOutputSchema"), dict) else packet.get("expectedOutputSchema")
            required_fields = list((schema or agent_result_packet_schema()).get("requiredFields", agent_result_packet_schema()["requiredFields"]))
            missing_fields = [field for field in required_fields if field not in payload]
            invalid_list_fields = [
                field
                for field in ["changedPaths", "blockers", "validation"]
                if field in payload and not isinstance(payload.get(field), list)
            ]
            if missing_fields or invalid_list_fields:
                blockers.append(
                    {
                        "kind": "agent_remediation_result_schema_invalid",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "missingFields": missing_fields,
                        "invalidListFields": invalid_list_fields,
                        "resultPath": str(result_path),
                    }
                )
            expected_identity = {
                "candidateId": packet.get("candidateId"),
                "shardId": shard.get("shardId"),
                "workBlockId": shard.get("workBlockId") or packet.get("workBlockId"),
                "actionId": packet.get("actionId"),
            }
            actual_identity = {
                "candidateId": payload.get("candidateId"),
                "shardId": payload.get("shardId"),
                "workBlockId": payload.get("workBlockId"),
                "actionId": payload.get("actionId"),
            }
            if actual_identity != expected_identity:
                blockers.append(
                    {
                        "kind": "agent_remediation_result_identity_mismatch",
                        "expected": expected_identity,
                        "actual": actual_identity,
                        "resultPath": str(result_path),
                    }
                )
            expected_tuple = packet.get("exactTuple")
            actual_tuple = payload.get("exactTuple") or {
                "candidateId": payload.get("candidateId"),
                "actionId": payload.get("actionId"),
                "evidenceHash": payload.get("evidenceHash"),
                "policyHash": payload.get("policyHash"),
                "pinnedRefs": payload.get("pinnedRefs"),
            }
            if actual_tuple != expected_tuple:
                blockers.append(
                    {
                        "kind": "agent_remediation_result_tuple_mismatch",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "expected": expected_tuple,
                        "actual": actual_tuple,
                        "resultPath": str(result_path),
                    }
                )
            status = str(payload.get("status") or "")
            payload_blockers = payload.get("blockers", [])
            failed_validation = [item for item in payload.get("validation", []) if isinstance(item, dict) and int(item.get("returncode", 0) or 0) != 0]
            if status != "resolved" or payload_blockers or failed_validation:
                blockers.append(
                    {
                        "kind": "blocked_agent_remediation_result",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "status": status,
                        "blockers": payload_blockers,
                        "validation": failed_validation,
                        "resultPath": str(result_path),
                    }
                )
    return {
        "status": "blocked" if blockers else "success",
        "packetCount": len(packets),
        "packets": packet_rows,
        "blockers": blockers,
    }


def repo_closed_artifact_path(repo_root: Path, config: Dict[str, Any], work_block_id: Optional[str]) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(work_block_id or "repo")).strip("-") or "repo"
    return closeout_state_root(repo_root, config) / "repo-closed" / ("%s.json" % safe_id)


def closeout_clean_truth_from_postcondition(postcondition: Dict[str, Any]) -> Dict[str, Any]:
    dirty_state = postcondition.get("dirtyState") if isinstance(postcondition.get("dirtyState"), dict) else {}
    stash_state = postcondition.get("stashState") if isinstance(postcondition.get("stashState"), dict) else {}
    branch_state = postcondition.get("branchState") if isinstance(postcondition.get("branchState"), dict) else {}
    worktree_state = postcondition.get("worktreeState") if isinstance(postcondition.get("worktreeState"), dict) else {}
    runtime_state = postcondition.get("runtimeArtifactState") if isinstance(postcondition.get("runtimeArtifactState"), dict) else {}
    remote_state = postcondition.get("remoteFeatureState") if isinstance(postcondition.get("remoteFeatureState"), dict) else {}
    agent_state = postcondition.get("agentRemediationState") if isinstance(postcondition.get("agentRemediationState"), dict) else {}
    freeze_state = postcondition.get("remediationFreezeState") if isinstance(postcondition.get("remediationFreezeState"), dict) else {}
    blockers = postcondition.get("blockers") if isinstance(postcondition.get("blockers"), list) else []
    blocker_kinds = [str(item.get("kind")) for item in blockers if isinstance(item, dict) and item.get("kind")]
    entries = dirty_state.get("entries") if isinstance(dirty_state.get("entries"), list) else []
    non_exempt = dirty_state.get("nonExempt") if isinstance(dirty_state.get("nonExempt"), list) else []
    untracked = dirty_state.get("untrackedNonExempt") if isinstance(dirty_state.get("untrackedNonExempt"), list) else []
    stashes = stash_state.get("stashes") if isinstance(stash_state.get("stashes"), list) else []
    stale_branches = branch_state.get("staleTransactionBranches") if isinstance(branch_state.get("staleTransactionBranches"), list) else []
    stale_worktrees = worktree_state.get("staleManagedWorktrees") if isinstance(worktree_state.get("staleManagedWorktrees"), list) else []
    linked_sibling_worktrees = worktree_state.get("linkedSiblingWorktrees") if isinstance(worktree_state.get("linkedSiblingWorktrees"), list) else []
    worktree_inspection = worktree_state.get("inspection") if isinstance(worktree_state.get("inspection"), dict) else {}
    worktree_inspection_failures = (
        worktree_inspection.get("inspectionFailures") if isinstance(worktree_inspection.get("inspectionFailures"), list) else []
    )
    orphaned_artifacts = runtime_state.get("orphanedArtifacts") if isinstance(runtime_state.get("orphanedArtifacts"), list) else []
    remote_features = remote_state.get("retainedRemoteFeatureRefs") if isinstance(remote_state.get("retainedRemoteFeatureRefs"), list) else []
    agent_blockers = agent_state.get("blockers") if isinstance(agent_state.get("blockers"), list) else []
    repo_closed = bool(postcondition.get("ok"))
    return {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "artifactKind": "closeoutCleanTruth",
        "authoritativeSource": "repoClosedPostcondition",
        "status": "clean" if repo_closed else "blocked",
        "repoClosed": repo_closed,
        "selectedWorkBlockId": postcondition.get("selectedWorkBlockId"),
        "targetRef": postcondition.get("targetRef"),
        "rawGit": {
            "clean": not entries,
            "statusEntryCount": len(entries),
            "entries": entries,
        },
        "policy": {
            "clean": repo_closed,
            "nonExemptDirtyCount": len(non_exempt),
            "untrackedNonExemptCount": len(untracked),
            "retainedForeignDirtyAllowed": bool(dirty_state.get("retainedForeignDirtyAllowed")),
            "retentionAudited": bool(dirty_state.get("retentionAudited")),
            "exemptStatusPatterns": dirty_state.get("exemptStatusPatterns") if isinstance(dirty_state.get("exemptStatusPatterns"), list) else [],
            "blockerKinds": blocker_kinds,
        },
        "cleanup": {
            "clean": not blockers,
            "stashClean": not stashes,
            "branchClean": not stale_branches,
            "worktreeClean": not stale_worktrees,
            "linkedSiblingWorktreeClean": not linked_sibling_worktrees,
            "worktreeInspectionClean": not worktree_inspection_failures,
            "runtimeArtifactClean": not orphaned_artifacts,
            "remoteFeatureClean": not remote_features,
            "agentQueueClosed": not agent_blockers,
            "remediationFreezeInactive": not bool(freeze_state.get("active")),
            "cleanupAudit": postcondition.get("cleanupAudit") if isinstance(postcondition.get("cleanupAudit"), dict) else {},
        },
        "blockers": blockers,
        "reason": postcondition.get("reason"),
    }


def repo_closed_postcondition_state(
    repo_root_arg: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    work_block_id: Optional[str] = None,
    finalize_result: Optional[Dict[str, Any]] = None,
    write_artifacts: bool = True,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = config or load_closeout_config(repo_root)
    hard_clean = config.get("hardClean", {})
    target = target_ref_for(repo_root, config)
    status_entries = parse_status_paths(repo_root)
    exempt_patterns = hard_clean.get("exemptStatusPatterns", [])
    non_exempt_status = [entry for entry in status_entries if not path_matches_any(str(entry["path"]), exempt_patterns)]
    untracked = [entry for entry in non_exempt_status if str(entry.get("status") or "").strip() == "??"]
    foreign_retention_allowed = bool(hard_clean.get("allowRetainedForeignDirtyAtCompletion", False))
    foreign_dirty_paths: set[str] = set()
    retention_audited = False
    detection_error: Optional[str] = None
    if work_block_id:
        try:
            detection = detect_work_block(repo_root, work_block_id=work_block_id)
            foreign_dirty_paths = {str(item.get("path")) for item in detection.get("foreignDirty", [])}
            retention_audited = audit_type_exists(repo_root, config, ["cleanup_retention", "retained_foreign_dirty"], work_block_id=work_block_id)
        except Exception as exc:
            if (finalize_result or {}).get("status") == "success" and "git ref is missing" in str(exc):
                foreign_dirty_paths = {str(item.get("path")) for item in (finalize_result or {}).get("foreignDirtyRetained", [])}
                retention_audited = audit_type_exists(repo_root, config, ["cleanup_retention", "retained_foreign_dirty"], work_block_id=work_block_id)
            else:
                detection_error = str(exc)
    status_paths = {str(item["path"]) for item in non_exempt_status}
    retained_foreign_status_allowed = bool(
        status_paths
        and (foreign_retention_allowed or (finalize_result or {}).get("status") == "success")
        and retention_audited
        and status_paths.issubset(foreign_dirty_paths)
    )
    stashes = stash_rows(repo_root)
    stale_branches = hard_clean_stale_transaction_branches(repo_root, config, str(target["targetBranch"]))
    stale_worktrees = hard_clean_stale_worktrees(repo_root, config)
    orphan_artifacts = orphaned_closeout_runtime_artifacts(repo_root, config)
    agent_remediation_state = agent_remediation_repo_closed_state(repo_root, config)
    freeze = remediation_freeze_status(repo_root, config, action="repo-closed-postcondition")
    remote_feature_plans: List[Dict[str, Any]] = []
    repo_sweep_error: Optional[str] = None
    try:
        remote_feature_plans = [
            item
            for item in repo_sweep_plan(repo_root, config).get("remoteFeaturePlans", [])
            if not item.get("protected")
        ]
    except Exception as exc:
        repo_sweep_error = str(exc)
    blockers: List[Dict[str, Any]] = []
    if non_exempt_status and not retained_foreign_status_allowed:
        blockers.append({"kind": "non_exempt_dirty_files", "entries": non_exempt_status})
    if untracked and not retained_foreign_status_allowed:
        blockers.append({"kind": "non_exempt_untracked_files", "entries": untracked})
    if bool(hard_clean.get("requireNoStash", True)) and stashes:
        blockers.append({"kind": "disallowed_stashes", "stashes": stashes})
    elif stashes and not audit_type_exists(repo_root, config, ["cleanup_retention", "snapshot_pruning", "stash_retention"]):
        blockers.append({"kind": "stash_retention_audit_missing", "stashes": stashes})
    worktree_inspection = worktree_inspection_state(repo_root, config)
    if stale_branches:
        blockers.append({"kind": "stale_transaction_branches", "branches": stale_branches})
    if bool(hard_clean.get("requireWorktreeInspection", True)) and worktree_inspection["inspectionFailures"]:
        blockers.append({"kind": "worktree_inspection_failed", "worktreeInspection": worktree_inspection})
    if bool(hard_clean.get("requireNoLinkedSiblingWorktrees", True)) and worktree_inspection["ordinaryLinkedWorktrees"]:
        blockers.append(
            {
                "kind": "linked_sibling_worktrees",
                "worktrees": worktree_inspection["ordinaryLinkedWorktrees"],
                "protectedLinkedWorktrees": worktree_inspection["protectedLinkedWorktrees"],
            }
        )
    if stale_worktrees:
        blockers.append({"kind": "stale_managed_worktrees", "worktrees": stale_worktrees})
    if orphan_artifacts:
        blockers.append({"kind": "orphaned_closeout_runtime_artifacts", "artifacts": orphan_artifacts})
    if agent_remediation_state["blockers"]:
        blockers.append({"kind": "agent_remediation_queue_not_closed", "agentRemediationState": agent_remediation_state})
    if freeze["active"]:
        blockers.append({"kind": "remediation_freeze_active", "freeze": freeze})
    if remote_feature_plans:
        blockers.append({"kind": "retained_remote_feature_refs", "remoteFeaturePlans": remote_feature_plans})
    if repo_sweep_error:
        blockers.append({"kind": "repo_closed_sweep_plan_error", "error": repo_sweep_error})
    if detection_error:
        blockers.append({"kind": "repo_closed_detection_error", "error": detection_error})
    ok = not blockers
    result = {
        "status": "success" if ok else "blocked",
        "ok": ok,
        "reason": None if ok else "repo_closed_postcondition_failed",
        "selectedWorkBlockId": work_block_id,
        "targetRef": target,
        "dirtyState": {
            "entries": status_entries,
            "nonExempt": non_exempt_status,
            "untrackedNonExempt": untracked,
            "exemptStatusPatterns": exempt_patterns,
            "retainedForeignDirtyAllowed": retained_foreign_status_allowed,
            "foreignDirtyPaths": sorted(foreign_dirty_paths),
            "retentionAudited": retention_audited,
        },
        "stashState": {"stashes": stashes, "requireNoStash": bool(hard_clean.get("requireNoStash", True))},
        "branchState": {"staleTransactionBranches": stale_branches},
        "worktreeState": {
            "staleManagedWorktrees": stale_worktrees,
            "inspection": worktree_inspection,
            "linkedSiblingWorktrees": worktree_inspection["ordinaryLinkedWorktrees"],
            "protectedLinkedWorktrees": worktree_inspection["protectedLinkedWorktrees"],
        },
        "runtimeArtifactState": {"orphanedArtifacts": orphan_artifacts},
        "agentRemediationState": agent_remediation_state,
        "remediationFreezeState": freeze,
        "remoteFeatureState": {"retainedRemoteFeatureRefs": remote_feature_plans},
        "cleanupAudit": {
            "retentionAudited": retention_audited,
            "artifactPath": normalize_rel(str(repo_closed_artifact_path(repo_root, config, work_block_id).relative_to(repo_root))),
        },
        "blockers": blockers,
        "finalizeResultStatus": (finalize_result or {}).get("status"),
    }
    if bool(hard_clean.get("unifiedTruthReport", {}).get("enabled", True)):
        result["closeoutCleanTruth"] = closeout_clean_truth_from_postcondition(result)
    if write_artifacts:
        write_json(repo_closed_artifact_path(repo_root, config, work_block_id), result)
        write_audit(repo_root, config, "repo_closed_postcondition", result, work_block_id=work_block_id, outcome="success" if ok else "blocked")
    return result


def verify_repo_closed_postcondition(
    repo_root_arg: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    work_block_id: Optional[str] = None,
    finalize_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return repo_closed_postcondition_state(
        repo_root_arg,
        config,
        work_block_id=work_block_id,
        finalize_result=finalize_result,
        write_artifacts=True,
    )


def finalize_retry_decision(
    config: Dict[str, Any],
    *,
    blocker_kind: str,
    evidence_hash_before: str,
    evidence_hash_after: str,
    pinned_refs_before_retry: Dict[str, Any],
    pins_match: bool,
    retry_number: int,
    seen_tuples: Iterable[str],
) -> Dict[str, Any]:
    loop = config.get("finalizeLoop", {})
    tuple_key = stable_hash({"blockerKind": blocker_kind, "evidenceHash": evidence_hash_before}, 16)
    symbolic = dict(loop.get("safeSecondOrderRepairs", {})).get(blocker_kind)
    if blocker_kind == "stale_review" and not symbolic and bool(config.get("autoQuorum", {}).get("allowStaleReviewRenewal", True)):
        symbolic = "renew_stale_review"
    max_retries = int(loop.get("maxRetries", 0) or 0)
    renewal_allowed = bool(loop.get("allowRepeatedBlockerEvidenceTupleRenewal", False))
    seen = set(seen_tuples)
    terminal_reason: Optional[str] = None
    should_retry = False
    if tuple_key in seen and not renewal_allowed:
        terminal_reason = "repeated_identical_blocker_evidence_tuple"
    elif not symbolic:
        terminal_reason = "no_safe_second_order_repair"
    elif retry_number >= max_retries:
        terminal_reason = "max_retries_exhausted"
    elif evidence_hash_before == evidence_hash_after:
        terminal_reason = "unchanged_evidence_after_repair"
    elif not pins_match:
        terminal_reason = "pinned_refs_changed_after_repair"
    else:
        should_retry = True
    return {
        "selectedWorkBlockId": None,
        "blockerKind": blocker_kind,
        "symbolicRepairAttempted": symbolic,
        "evidenceHashBeforeRepair": evidence_hash_before,
        "evidenceHashAfterRepair": evidence_hash_after,
        "pinnedRefsBeforeRetry": pinned_refs_before_retry,
        "retryNumber": retry_number,
        "blockerEvidenceTuple": tuple_key,
        "shouldRetry": should_retry,
        "terminalReason": terminal_reason,
    }


def finalize_attempt_snapshot(repo_root: Path, config: Dict[str, Any], work_block_id: Optional[str]) -> Dict[str, Any]:
    detection = detect_work_block(repo_root, work_block_id=work_block_id)
    evidence = finalize_evidence(config, detection)
    return {
        "workBlockId": detection["workBlockId"],
        "detection": detection,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": detection["pinnedRefs"],
    }


def finalize_retry_ledger_path(repo_root: Path, config: Dict[str, Any], work_block_id: str) -> Path:
    return work_block_dir(repo_root, config, work_block_id) / "finalize-retry-ledger.json"


def load_finalize_retry_ledger(repo_root: Path, config: Dict[str, Any], work_block_id: str) -> List[Dict[str, Any]]:
    path = finalize_retry_ledger_path(repo_root, config, work_block_id)
    rows = read_json(path, [])
    return rows if isinstance(rows, list) else []


def save_finalize_retry_ledger(repo_root: Path, config: Dict[str, Any], work_block_id: str, ledger: List[Dict[str, Any]]) -> None:
    write_json(finalize_retry_ledger_path(repo_root, config, work_block_id), ledger)


def write_finalize_retry_audit(repo_root: Path, config: Dict[str, Any], work_block_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    audit_type = str(config.get("finalizeLoop", {}).get("retryAuditType") or "finalize_retry")
    payload = {**decision, "selectedWorkBlockId": work_block_id}
    row = write_audit(repo_root, config, audit_type, payload, work_block_id=work_block_id, outcome="recorded" if decision["shouldRetry"] else "blocked")
    append_event(
        repo_root,
        config,
        work_block_id,
        {
            "event": "finalize_retry",
            "retryNumber": decision["retryNumber"],
            "blockerKind": decision["blockerKind"],
            "shouldRetry": decision["shouldRetry"],
            "terminalReason": decision["terminalReason"],
        },
    )
    return row


def validate_work_block_start_head_integrated(
    repo_root: Path,
    config: Dict[str, Any],
    detection: Dict[str, Any],
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    bootstrap = config.get("workBlockBootstrap", {})
    if not bool(bootstrap.get("requireIntegratedStartHeadForFinalize", True)):
        return None
    start_head = str(manifest.get("startHead") or "")
    target_head = str(detection.get("targetHead") or "")
    if start_head and target_head and is_ancestor(repo_root, start_head, target_head):
        return None
    target_branch = str(detection.get("targetBranch") or config.get("git", {}).get("targetBranch") or "master")
    return {
        "reason": "work_block_base_not_integrated",
        "workBlockId": detection.get("workBlockId"),
        "branch": detection.get("branch"),
        "startHead": start_head,
        "targetHead": target_head,
        "targetBranch": target_branch,
        "policy": "workBlockBootstrap.requireIntegratedStartHeadForFinalize",
        "recoveryCommand": (
            "Switch to a clean approved target base, then start a fresh work block: "
            "git switch %s; %s"
            % (
                target_branch,
                closeout_script_command("start-work-block.ps1", ["-Summary", "<human commit subject>"], config),
            )
        ),
    }


def content_review_range_tokens(start_head: str, feature_head: str) -> List[str]:
    starts = {start_head, start_head[:12], start_head[:8]}
    features = {feature_head, feature_head[:12], feature_head[:8]}
    tokens = sorted(
        {
            f"{start}..{feature}"
            for start in starts
            for feature in features
            if start and feature
        },
        key=lambda value: (len(value), value),
    )
    return tokens


def coordination_entries(text: str) -> List[Dict[str, Any]]:
    headings = list(re.finditer(r"(?m)^### \[[^\]]+\][^\n]*$", text))
    entries: List[Dict[str, Any]] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.start() : end]
        entries.append({"offset": match.start(), "heading": match.group(0), "body": body})
    return entries


def coordination_entry_verdict(entry_body: str) -> str:
    match = re.search(r"(?im)^\s*Verdict:\s*(.+?)\s*$", entry_body)
    if match:
        return match.group(1).strip()
    return ""


def coordination_entry_range(entry_body: str) -> str:
    match = re.search(r"(?im)^\s*Range:\s*`?([^`\r\n]+?)`?\s*$", entry_body)
    if match:
        return match.group(1).strip()
    return ""


# Coordination headings follow one universal grammar across the ledger:
#   ### [<timestamp>] ACTOR - KIND (free-text description)
# The actor/kind must be captured from that grammar, never substring-matched against
# the whole heading: the free-text portion routinely names other lanes and kinds
# (189 live instances at adoption time), so a substring test lets an entry's own
# prose satisfy actor/kind it does not declare.
COORDINATION_HEADING_RE = re.compile(
    r"^### \[[^\]]+\]\s*(?P<actor>[A-Za-z0-9_]+)\s*[-–—]\s*(?P<kind>[A-Za-z0-9_]+)\b"
)

COORDINATION_SEAT_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def coordination_entry_actor_kind(heading: str) -> Tuple[str, str]:
    match = COORDINATION_HEADING_RE.match(str(heading or ""))
    if not match:
        return "", ""
    return match.group("actor").upper(), match.group("kind").upper()


def coordination_entry_seat_session(entry_body: str) -> str:
    # LAST Seat: line wins (LANE-4 review B3): an entry quoting another entry's Seat line
    # above its own footer must not mis-attribute itself to the quoted seat.
    matches = re.findall(r"(?im)^\s*Seat:\s*(.+)$", entry_body)
    if not matches:
        return ""
    guid = COORDINATION_SEAT_GUID_RE.search(matches[-1])
    return guid.group(0).lower() if guid else ""


def coordination_entry_matches(
    entry: Dict[str, Any],
    *,
    actor: str,
    kind: str,
    range_token: str,
    additional_actors: Sequence[str] = (),
) -> bool:
    heading_actor, heading_kind = coordination_entry_actor_kind(str(entry.get("heading") or ""))
    allowed_actors = {actor.upper()} | {str(extra).upper() for extra in additional_actors if str(extra).strip()}
    if heading_actor not in allowed_actors or heading_kind != kind.upper():
        return False
    return coordination_entry_range(str(entry.get("body") or "")) == range_token


def content_review_gate_block(
    config: Dict[str, Any],
    detection: Dict[str, Any],
    *,
    reason: str,
    coordination_file: str,
    range_tokens: Sequence[str],
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    gate = config.get("contentReviewGate", {})
    target_branch = str(detection.get("targetBranch") or config.get("git", {}).get("targetBranch") or "master")
    primary_range = range_tokens[-1] if range_tokens else ""
    # Derive the expected handoff/review actors and tokens from config so the
    # recovery guidance stays correct after a role swap (handoff/review actors
    # are configurable; do not hard-code CODEX/CLAUDE here).
    handoff_actor = str(gate.get("handoffActor") or "CODEX")
    handoff_kind = str(gate.get("handoffKind") or "HANDOFF")
    review_actor = str(gate.get("reviewActor") or "CLAUDE")
    review_kind = str(gate.get("reviewKind") or "REVIEW")
    approve_tokens = [str(token).upper() for token in gate.get("approveTokens", ["APPROVE"])]
    blocking_tokens = [str(token).upper() for token in gate.get("blockingTokens", ["CHANGES_REQUESTED", "BLOCKER"])]
    approve_token = approve_tokens[0] if approve_tokens else "APPROVE"
    range_display = primary_range or "<targetHead>..<featureHead>"
    authorized_sessions = [str(s).strip() for s in gate.get("authorizedReviewSessions", []) if str(s).strip()]
    seat_note = ""
    if authorized_sessions:
        seat_note = (
            " The review entry must ALSO carry a 'Seat: <GUID>' line whose GUID is one of "
            "contentReviewGate.authorizedReviewSessions; an approving verdict without it blocks as "
            "content_approval_unattributed_verdict, and an unlisted GUID blocks as "
            "content_approval_unauthorized_seat."
        )
    payload = {
        "reason": reason,
        "workBlockId": detection.get("workBlockId"),
        "branch": detection.get("branch"),
        "targetBranch": target_branch,
        "targetHead": detection.get("targetHead"),
        "featureHead": detection.get("featureHead"),
        "range": primary_range,
        "rangeTokens": list(range_tokens),
        "coordinationFile": coordination_file,
        "policy": "contentReviewGate.requireClaudeApprovalForFinalize",
        "expectedEntryFormat": {
            "canonicalRange": range_display,
            "requiredHeadingFormat": "### [<timestamp>] ACTOR - KIND (free text)",
            "handoffHeadingContains": [handoff_actor, handoff_kind],
            "reviewHeadingContains": [review_actor, review_kind],
            "requiredRangeLine": "Range: `%s`" % range_display,
            "requiredVerdictLine": "Verdict: %s" % approve_token,
            "approveTokens": approve_tokens,
            "blockingTokens": blocking_tokens,
            "notes": (
                "Append a '%s ... %s' entry, then a LATER '%s ... %s' entry. Headings are PARSED as "
                "'### [<timestamp>] ACTOR - KIND (free text)': the DECLARED actor and kind must equal the "
                "configured tokens case-insensitively (free-text prose no longer matches, and a heading that "
                "does not parse fails closed). The review entry's Range: line value must EXACTLY equal "
                "the canonical range above -- short 8/12-char ranges no longer match. The Verdict: token "
                "must EXACTLY equal an approve/blocking token (e.g. 'Verdict: %s'); a 'Verdict: %s -- <range>' "
                "suffix form no longer counts.%s"
                % (handoff_actor, handoff_kind, review_actor, review_kind, approve_token, approve_token, seat_note)
            ),
        },
        "recoveryCommand": (
            "Append a %s %s for range %s to %s, then a LATER %s %s for the SAME range with a bare "
            "'Verdict: %s' line%s, then rerun %s"
            % (
                handoff_actor,
                handoff_kind,
                range_display,
                coordination_file,
                review_actor,
                review_kind,
                approve_token,
                (" and a 'Seat: <authorized GUID>' line" if authorized_sessions else ""),
                closeout_script_command("work-block-complete.ps1", ["-Finalize"], config),
            )
        ),
    }
    if authorized_sessions:
        payload["expectedEntryFormat"]["requiredSeatLine"] = "Seat: <one of contentReviewGate.authorizedReviewSessions>"
    if detail:
        payload["detail"] = detail
    if gate:
        payload["gate"] = {
            "handoffActor": gate.get("handoffActor"),
            "handoffKind": gate.get("handoffKind"),
            "reviewActor": gate.get("reviewActor"),
            "reviewKind": gate.get("reviewKind"),
            "requireHandoff": gate.get("requireHandoff"),
        }
    return payload


def validate_content_review_approval_for_finalize(
    repo_root: Path,
    config: Dict[str, Any],
    detection: Dict[str, Any],
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    gate = config.get("contentReviewGate", {})
    if not isinstance(gate, dict) or not bool(gate.get("requireClaudeApprovalForFinalize", False)):
        return None
    coordination_file = str(gate.get("coordinationFile") or ".claude-state/coordination/gpu-lane-impl-review-sync.md")
    start_head = str(manifest.get("startHead") or detection.get("targetHead") or "")
    feature_head = str(detection.get("featureHead") or "")
    range_tokens = content_review_range_tokens(start_head, feature_head)
    canonical_range = range_tokens[-1] if range_tokens else ""
    if not range_tokens:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_range_unknown",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
        )
    # GATE-ID-4: resolve the ledger against the MAIN worktree root, not this work block's
    # worktree, so every block reads and writes the same file by construction.
    ledger_root = canonical_worktree_root(repo_root)
    if ledger_root is None:
        # Fail closed. Falling back to repo_root here would silently restore the
        # per-worktree behaviour this change removes, while looking fixed.
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_ledger_root_unresolved",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"repoRoot": str(repo_root)},
        )
    coordination_path = (ledger_root / coordination_file).resolve()
    try:
        coordination_path.relative_to(ledger_root.resolve())
    except ValueError:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_coordination_path_escape",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"resolvedPath": str(coordination_path)},
        )
    if not coordination_path.exists():
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_file_missing",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
        )
    text = coordination_path.read_text(encoding="utf-8")
    entries = coordination_entries(text)
    # (M) supersession-axis fail-closed (LANE-4 review R1): an entry that NAMES this range
    # but whose heading does not parse under the grammar cannot be classified, so the
    # "latest decisive verdict" ordering cannot be trusted -- a Markdown-decorated
    # superseding BLOCK would otherwise be silently dropped and a stale APPROVE would
    # release. Scoped to range-naming entries, this is a no-op on the live ledger
    # (0 non-parsing headings today) and can only fail closed.
    unparsable = [
        entry
        for entry in entries
        if coordination_entry_actor_kind(str(entry.get("heading") or "")) == ("", "")
        and coordination_entry_range(str(entry.get("body") or "")) == canonical_range
    ]
    if unparsable:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_unparsable_heading",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"unparsableHeadings": [str(entry.get("heading") or "") for entry in unparsable]},
        )
    handoff_actor = str(gate.get("handoffActor") or "CODEX")
    handoff_kind = str(gate.get("handoffKind") or "HANDOFF")
    review_actor = str(gate.get("reviewActor") or "CLAUDE")
    review_kind = str(gate.get("reviewKind") or "REVIEW")
    require_handoff = bool(gate.get("requireHandoff", True))
    additional_handoff_actors = [
        str(actor_name) for actor_name in gate.get("additionalHandoffActors", []) if str(actor_name).strip()
    ]
    handoffs = [
        entry
        for entry in entries
        if coordination_entry_matches(
            entry,
            actor=handoff_actor,
            kind=handoff_kind,
            range_token=canonical_range,
            additional_actors=additional_handoff_actors,
        )
    ]
    if require_handoff and not handoffs:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_missing_handoff",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
        )
    handoff_offset = int(handoffs[-1]["offset"]) if handoffs else -1
    reviews = [
        entry
        for entry in entries
        if int(entry["offset"]) > handoff_offset
        and coordination_entry_matches(entry, actor=review_actor, kind=review_kind, range_token=canonical_range)
    ]
    if not reviews:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_missing",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"handoffOffset": handoff_offset},
        )
    blocking_tokens = [str(token).upper() for token in gate.get("blockingTokens", ["CHANGES_REQUESTED", "BLOCKER"])]
    approve_tokens = [str(token).upper() for token in gate.get("approveTokens", ["APPROVE"])]
    decisive_reviews = []
    for entry in reviews:
        verdict = coordination_entry_verdict(str(entry.get("body") or ""))
        verdict_upper = verdict.upper()
        if verdict_upper in blocking_tokens or verdict_upper in approve_tokens:
            decisive_reviews.append({"entry": entry, "verdict": verdict})
    if not decisive_reviews:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_not_approved",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"latestReviewHeading": reviews[-1].get("heading"), "verdict": coordination_entry_verdict(str(reviews[-1].get("body") or ""))},
        )
    latest = decisive_reviews[-1]["entry"]
    verdict = str(decisive_reviews[-1]["verdict"])
    verdict_upper = verdict.upper()
    if verdict_upper in blocking_tokens:
        return content_review_gate_block(
            config,
            detection,
            reason="content_approval_blocked",
            coordination_file=coordination_file,
            range_tokens=range_tokens,
            detail={"latestReviewHeading": latest.get("heading"), "verdict": verdict},
        )
    if verdict_upper in approve_tokens:
        # Identity check (reader-side): the DECISIVE APPROVING entry must be attributable
        # to an authorized review session. Enforcement engages when the tracked config
        # carries a non-empty allowlist; the allowlist lives in closeout.config.json
        # (not the gitignored seat registry) because this validator runs inside work-block
        # worktrees where .claude-state does not exist. Blocking verdicts deliberately
        # skip this check: an unattributed block still blocks (fail-closed direction).
        authorized_sessions = [
            str(session).strip().lower()
            for session in gate.get("authorizedReviewSessions", [])
            if str(session).strip()
        ]
        if authorized_sessions:
            seat_session = coordination_entry_seat_session(str(latest.get("body") or ""))
            if not seat_session:
                return content_review_gate_block(
                    config,
                    detection,
                    reason="content_approval_unattributed_verdict",
                    coordination_file=coordination_file,
                    range_tokens=range_tokens,
                    detail={
                        "latestReviewHeading": latest.get("heading"),
                        "verdict": verdict,
                        "requiredSeatLine": "Seat: <registered review session GUID>",
                    },
                )
            if seat_session not in authorized_sessions:
                return content_review_gate_block(
                    config,
                    detection,
                    reason="content_approval_unauthorized_seat",
                    coordination_file=coordination_file,
                    range_tokens=range_tokens,
                    detail={
                        "latestReviewHeading": latest.get("heading"),
                        "verdict": verdict,
                        "seatSession": seat_session,
                        "authorizedReviewSessions": authorized_sessions,
                    },
                )
        return None
    return content_review_gate_block(
        config,
        detection,
        reason="content_approval_not_approved",
        coordination_file=coordination_file,
        range_tokens=range_tokens,
        detail={"latestReviewHeading": latest.get("heading"), "verdict": verdict},
    )


def _finalize_work_block_once(
    repo_root_arg: Path,
    *,
    work_block_id: Optional[str] = None,
    expected_pinned_refs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    tooling = verify_closeout_tooling_current(repo_root, config, plan_only=True)
    if not tooling["ok"]:
        return {"status": "blocked", "reason": "closeout_tooling_stale", "tooling": tooling}
    detection = detect_work_block(repo_root, work_block_id=work_block_id)
    block_id = detection["workBlockId"]
    update_manifest(repo_root, config, block_id, {"state": "finalizing", "completedAt": utc_now()})
    manifest = load_manifest(repo_root, config, block_id)
    commit_subject = delivered_work_commit_subject(repo_root, detection, manifest)
    base_guard = validate_work_block_start_head_integrated(repo_root, config, detection, manifest)
    if base_guard:
        write_audit(repo_root, config, "work_block_base_not_integrated", base_guard, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": "work_block_base_not_integrated"})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "work_block_base_not_integrated"})
        return {"status": "blocked", **base_guard, "detection": detection}
    if expected_pinned_refs is not None and expected_pinned_refs != detection["pinnedRefs"]:
        payload = {"expectedPinnedRefs": expected_pinned_refs, "actualPinnedRefs": detection["pinnedRefs"]}
        write_audit(repo_root, config, "stale_refs", payload, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": "stale_refs"})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "stale_refs"})
        return {"status": "blocked", "reason": "stale_refs", **payload}
    repair = repair_eligibility(repo_root, work_block_id=block_id)
    if repair["status"] != "repaired":
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": "repair_blocked"})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "repair_blocked"})
        return {"status": "blocked", "reason": "repair_blocked", "repair": repair, "detection": detection}
    detection = detect_work_block(repo_root, work_block_id=block_id)
    from .candidate_acceptance import validate_for_finalize as validate_candidate_acceptance_for_finalize

    acceptance_guard = validate_candidate_acceptance_for_finalize(repo_root, config, detection)
    if acceptance_guard:
        reason = str(acceptance_guard["reason"])
        write_audit(repo_root, config, reason, acceptance_guard, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": reason})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": reason})
        return {"status": "blocked", **acceptance_guard, "detection": detection}
    content_guard = validate_content_review_approval_for_finalize(repo_root, config, detection, manifest)
    if content_guard:
        reason = str(content_guard["reason"])
        write_audit(repo_root, config, reason, content_guard, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": reason})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": reason})
        return {"status": "blocked", **content_guard, "detection": detection}
    evidence = finalize_evidence(config, detection)
    evidence_hash = stable_hash(evidence)
    candidate_id = finalize_candidate_id(block_id)
    action_id = finalize_action_id()
    quorum = check_review_quorum(
        repo_root,
        config,
        candidate_id=candidate_id,
        action_id=action_id,
        evidence_hash=evidence_hash,
        pinned_refs=detection["pinnedRefs"],
    )
    stale_review_renewal = quorum["reason"] == "stale_review" and bool(config.get("autoQuorum", {}).get("allowStaleReviewRenewal", True))
    if not quorum["ok"] and (quorum["reason"] == "review_quorum_missing" or stale_review_renewal):
        quorum_result = ensure_autonomous_quorum(
            repo_root,
            config,
            candidate_id=candidate_id,
            action_id=action_id,
            evidence_hash=evidence_hash,
            pinned_refs=detection["pinnedRefs"],
            evidence=evidence,
            action_class="repo_sweep_clean_integrate",
            blockers=[],
        )
        quorum = quorum_result["quorum"]
    if not quorum["ok"]:
        audit_type = "stale_review" if quorum["reason"] == "stale_review" else "review_quorum_blocked"
        payload = {"candidateId": candidate_id, "actionId": action_id, "evidenceHash": evidence_hash, "quorum": quorum}
        write_audit(repo_root, config, audit_type, payload, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": quorum["reason"]})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": quorum["reason"]})
        return {"status": "blocked", "reason": quorum["reason"], "quorum": quorum, "evidenceHash": evidence_hash}
    target_branch = detection["targetBranch"]
    target = target_ref_for(repo_root, config)
    feature_head = detection["featureHead"]
    if target["head"] != detection["targetHead"]:
        payload = {"expectedTargetHead": detection["targetHead"], "actualTargetHead": target["head"]}
        write_audit(repo_root, config, "stale_refs", payload, work_block_id=block_id, outcome="blocked")
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": "stale_refs"})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "stale_refs"})
        return {"status": "blocked", "reason": "stale_refs", **payload}
    runtime_lifecycle = {
        "stopBeforePromotion": stop_runtime_services_before_promotion(repo_root, config, work_block_id=block_id),
        "restartAfterCleanPromotion": None,
    }
    if runtime_lifecycle["stopBeforePromotion"]["status"] != "success":
        append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": runtime_lifecycle["stopBeforePromotion"]["reason"]})
        update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": runtime_lifecycle["stopBeforePromotion"]["reason"]})
        return {"status": "blocked", "reason": runtime_lifecycle["stopBeforePromotion"]["reason"], "runtimeLifecycle": runtime_lifecycle}
    already_integrated = is_ancestor(repo_root, feature_head, target["head"])
    integration_path: Optional[Path] = None
    new_target_head = target["head"]
    push_result: Optional[Dict[str, Any]] = None
    recovery: Optional[Dict[str, Any]] = None
    local_update: Optional[Dict[str, Any]] = None
    target_worktree_preflight: Optional[Dict[str, Any]] = None
    if already_integrated:
        local_head = rev_parse(repo_root, f"refs/heads/{target_branch}", required=False)
        if local_head != target["head"]:
            recovery = update_local_target(repo_root, target_branch, target["head"])
            write_audit(repo_root, config, "partial_push_recovery", recovery, work_block_id=block_id, outcome="success" if recovery["returncode"] == 0 else "blocked")
            if recovery["returncode"] != 0:
                update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "partial_push_recovery_failed"})
                return {"status": "blocked", "reason": "partial_push_recovery_failed", "recovery": recovery, "runtimeLifecycle": runtime_lifecycle}
    else:
        integration_path = closeout_state_root(repo_root, config) / "integration-worktrees" / ("%s-%s" % (block_id, uuid.uuid4().hex[:8]))
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        add = run_git_longpaths(repo_root, ["worktree", "add", "--detach", str(integration_path), target["head"]])
        if add.returncode != 0:
            payload = {"operation": "worktree_add", "returncode": add.returncode, "stderr": add.stderr[-4000:]}
            write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "integration_worktree_failed"})
            return {"status": "blocked", "reason": "integration_worktree_failed", "detail": payload, "runtimeLifecycle": runtime_lifecycle}
        merge = run_git(
            integration_path,
            [
                "merge",
                "--no-ff",
                "-m",
                closeout_merge_commit_message(current_branch(repo_root) or block_id, target_branch, work_summary=commit_subject),
                feature_head,
            ],
        )
        if merge.returncode != 0:
            payload = {"operation": "merge", "returncode": merge.returncode, "stdout": merge.stdout[-4000:], "stderr": merge.stderr[-4000:]}
            write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
            remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "merge_failed"})
            return {"status": "blocked", "reason": "merge_failed", "detail": payload, "runtimeLifecycle": runtime_lifecycle}
        merged_head = git_stdout(integration_path, ["rev-parse", "HEAD"])
        final_range = integration_range_evidence(integration_path, target["head"], merged_head)
        if final_range["diffCheckReturncode"] != 0:
            payload = {
                "operation": "git_diff_check",
                "range": final_range["range"],
                "returncode": final_range["diffCheckReturncode"],
                "stdout": final_range["diffCheckStdout"][-4000:],
                "stderr": final_range["diffCheckStderr"][-4000:],
            }
            write_audit(repo_root, config, "validation_failure", payload, work_block_id=block_id, outcome="blocked")
            remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "diff_check_failed"})
            return {"status": "blocked", "reason": "diff_check_failed", "detail": payload, "runtimeLifecycle": runtime_lifecycle}
        final_mismatches: Dict[str, Any] = {}
        accepted: Dict[str, Any] = {}
        from .candidate_acceptance import candidate_acceptance_enforced

        try:
            acceptance_enforced = candidate_acceptance_enforced(repo_root, config, detection)
        except HygieneError as exc:
            payload = {"operation": "candidate_acceptance_policy", "detail": str(exc)}
            write_audit(repo_root, config, "candidate_acceptance_policy_invalid", payload, work_block_id=block_id, outcome="blocked")
            remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "candidate_acceptance_policy_invalid"})
            return {
                "status": "blocked",
                "reason": "candidate_acceptance_policy_invalid",
                "detail": payload,
                "runtimeLifecycle": runtime_lifecycle,
            }
        if acceptance_enforced:
            from .candidate_acceptance import (
                final_integration_mismatches as candidate_acceptance_final_integration_mismatches,
                latest_summary as candidate_acceptance_latest_summary,
            )

            accepted = candidate_acceptance_latest_summary(repo_root, config)
            final_mismatches = candidate_acceptance_final_integration_mismatches(accepted, final_range)
        if final_mismatches:
            payload = {"operation": "candidate_acceptance_final_tree", "mismatches": final_mismatches, "range": final_range}
            write_audit(repo_root, config, "candidate_acceptance_final_tree_mismatch", payload, work_block_id=block_id, outcome="blocked")
            remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "candidate_acceptance_final_tree_mismatch"})
            return {
                "status": "blocked",
                "reason": "candidate_acceptance_final_tree_mismatch",
                "detail": payload,
                "runtimeLifecycle": runtime_lifecycle,
            }
        validations = run_validations(
            repo_root,
            config,
            integration_path,
            changed_paths=detection.get("committedDeltaPaths"),
            work_block_id=block_id,
        )
        failed_validation = next((item for item in validations if item["returncode"] != 0), None)
        if failed_validation:
            payload = {"validations": validations}
            write_audit(repo_root, config, "validation_failure", payload, work_block_id=block_id, outcome="blocked")
            if bool(config.get("cleanupPolicy", {}).get("removeIntegrationWorktreeAfterFailure", True)):
                remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "validation_failed"})
            return {"status": "blocked", "reason": "validation_failed", "validations": validations, "runtimeLifecycle": runtime_lifecycle}
        new_target_head = merged_head
        if target["mode"] == "remote":
            target_worktree_preflight = target_worktree_update_preflight(repo_root, target_branch, new_target_head)
            if target_worktree_preflight["status"] != "success":
                payload = {
                    "operation": "target_worktree_preflight",
                    "reason": target_worktree_preflight.get("reason"),
                    "preflight": target_worktree_preflight,
                }
                write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
                remove_worktree(repo_root, integration_path)
                update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "target_worktree_not_ready_before_push"})
                return {
                    "status": "blocked",
                    "reason": "target_worktree_not_ready_before_push",
                    "detail": payload,
                    "runtimeLifecycle": runtime_lifecycle,
                }
            push = run_git(integration_path, ["push", str(target.get("remote")), f"HEAD:{target_branch}"])
            push_result = {"remote": target.get("remote"), "targetBranch": target_branch, "returncode": push.returncode, "stdout": push.stdout[-4000:], "stderr": push.stderr[-4000:]}
            if push.returncode != 0:
                recovery = repair_target_push_failure(
                    repo_root,
                    config,
                    target_branch=target_branch,
                    remote=str(target.get("remote")),
                    attempted_head=new_target_head,
                    push_result=push_result,
                    work_block_id=block_id,
                )
                if recovery["status"] != "success":
                    remove_worktree(repo_root, integration_path)
                    append_event(repo_root, config, block_id, {"event": "finalize_blocked", "reason": recovery["reason"]})
                    update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": recovery["reason"]})
                    return {"status": "blocked", "reason": recovery["reason"], "push": push_result, "recovery": recovery, "runtimeLifecycle": runtime_lifecycle}
                new_target_head = str(recovery["targetHeadAfter"])
                local_update = recovery.get("localTargetUpdate")
        if local_update is None:
            local_update = update_local_target(repo_root, target_branch, new_target_head)
        if local_update["returncode"] != 0:
            write_audit(repo_root, config, "partial_push_recovery", local_update, work_block_id=block_id, outcome="blocked")
            remove_worktree(repo_root, integration_path)
            update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "local_target_update_failed"})
            return {"status": "blocked", "reason": "local_target_update_failed", "localUpdate": local_update, "push": push_result, "runtimeLifecycle": runtime_lifecycle}
    cleanup = cleanup_after_success(repo_root, config, detection, new_target_head=new_target_head, integration_path=integration_path)
    if detection.get("foreignDirty"):
        write_audit(
            repo_root,
            config,
            "retained_foreign_dirty",
            {"selectedWorkBlockId": block_id, "foreignDirtyRetained": detection["foreignDirty"]},
            work_block_id=block_id,
            outcome="retained",
        )
    success_payload = {
        "selectedWorkBlockId": block_id,
        "candidateId": candidate_id,
        "actionId": action_id,
        "evidenceHash": evidence_hash,
        "quorum": quorum,
        "featureHead": feature_head,
        "targetHeadBefore": detection["targetHead"],
        "targetHeadAfter": new_target_head,
        "alreadyIntegrated": already_integrated,
        "targetWorktreePreflight": target_worktree_preflight,
        "push": push_result,
        "recovery": recovery,
        "cleanup": cleanup,
        "runtimeLifecycle": runtime_lifecycle,
        "foreignDirtyRetained": detection["foreignDirty"],
        "workBlockSelection": detection.get("workBlockSelection"),
    }
    write_audit(repo_root, config, "success", success_payload, work_block_id=block_id, outcome="success")
    append_event(repo_root, config, block_id, {"event": "finalize_success", "targetHeadAfter": new_target_head})
    update_manifest(repo_root, config, block_id, {"state": "finalized", "finalizedAt": utc_now(), "targetHeadAfter": new_target_head})
    return {"status": "success", **success_payload}


def finalize_work_block(
    repo_root_arg: Path,
    *,
    work_block_id: Optional[str] = None,
    expected_pinned_refs: Optional[Dict[str, Any]] = None,
    auto_approve: bool = False,
    require_repo_closed: bool = False,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="finalize", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    require_repo_closed = bool(require_repo_closed or config.get("hardClean", {}).get("requireForCompletion", False))
    if not bool(config.get("finalizeLoop", {}).get("enabled", True)):
        result = _finalize_work_block_once(repo_root, work_block_id=work_block_id, expected_pinned_refs=expected_pinned_refs)
        if auto_approve:
            result["autoApproveRequested"] = True
        if require_repo_closed and result.get("status") == "success":
            block_id = str(work_block_id or result.get("selectedWorkBlockId") or "")
            postcondition = verify_repo_closed_postcondition(repo_root, config, work_block_id=block_id or None, finalize_result=result)
            result["repoClosedPostcondition"] = postcondition
            if not postcondition["ok"]:
                if block_id:
                    update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": "repo_closed_postcondition_failed"})
                return {**result, "status": "blocked", "reason": "repo_closed_postcondition_failed", "finalizeStatus": "success"}
            restart = restart_runtime_services_after_clean_promotion(repo_root, config, result.get("runtimeLifecycle", {}).get("stopBeforePromotion"), work_block_id=block_id or None)
            result.setdefault("runtimeLifecycle", {})["restartAfterCleanPromotion"] = restart
            if restart["status"] != "success":
                if block_id:
                    update_manifest(repo_root, config, block_id, {"state": "blocked", "blockedReason": restart["reason"]})
                return {**result, "status": "blocked", "reason": restart["reason"], "finalizeStatus": "success"}
        return result
    seen_tuples: set[str] = set()
    ledger: List[Dict[str, Any]] = []
    retry_number = 0
    selected_work_block_id = work_block_id
    while True:
        before = finalize_attempt_snapshot(repo_root, config, selected_work_block_id)
        selected_work_block_id = str(before["workBlockId"])
        if not ledger:
            ledger = load_finalize_retry_ledger(repo_root, config, selected_work_block_id)
            seen_tuples = {str(item.get("blockerEvidenceTuple")) for item in ledger if item.get("blockerEvidenceTuple")}
            retry_number = len(ledger)
        result = _finalize_work_block_once(repo_root, work_block_id=selected_work_block_id, expected_pinned_refs=expected_pinned_refs)
        if result.get("status") == "success":
            if ledger:
                result["finalizeRetryLedger"] = ledger
            if auto_approve:
                result["autoApproveRequested"] = True
            if require_repo_closed:
                postcondition = verify_repo_closed_postcondition(repo_root, config, work_block_id=selected_work_block_id, finalize_result=result)
                result["repoClosedPostcondition"] = postcondition
                if not postcondition["ok"]:
                    update_manifest(repo_root, config, selected_work_block_id, {"state": "blocked", "blockedReason": "repo_closed_postcondition_failed"})
                    return {**result, "status": "blocked", "reason": "repo_closed_postcondition_failed", "finalizeStatus": "success"}
                restart = restart_runtime_services_after_clean_promotion(
                    repo_root,
                    config,
                    result.get("runtimeLifecycle", {}).get("stopBeforePromotion"),
                    work_block_id=selected_work_block_id,
                )
                result.setdefault("runtimeLifecycle", {})["restartAfterCleanPromotion"] = restart
                if restart["status"] != "success":
                    update_manifest(repo_root, config, selected_work_block_id, {"state": "blocked", "blockedReason": restart["reason"]})
                    return {**result, "status": "blocked", "reason": restart["reason"], "finalizeStatus": "success"}
            return result
        blocker_kind = str(result.get("reason") or "blocked")
        try:
            after = finalize_attempt_snapshot(repo_root, config, selected_work_block_id)
            evidence_hash_after = str(after["evidenceHash"])
            pins_after = after["pinnedRefs"]
        except Exception as exc:
            evidence_hash_after = str(before["evidenceHash"])
            pins_after = before["pinnedRefs"]
            result["retrySnapshotError"] = str(exc)
        pins_match = expected_pinned_refs is None or expected_pinned_refs == pins_after
        decision = finalize_retry_decision(
            config,
            blocker_kind=blocker_kind,
            evidence_hash_before=str(before["evidenceHash"]),
            evidence_hash_after=evidence_hash_after,
            pinned_refs_before_retry=before["pinnedRefs"],
            pins_match=pins_match,
            retry_number=retry_number,
            seen_tuples=seen_tuples,
        )
        decision["selectedWorkBlockId"] = selected_work_block_id
        write_finalize_retry_audit(repo_root, config, selected_work_block_id, decision)
        ledger.append(decision)
        save_finalize_retry_ledger(repo_root, config, selected_work_block_id, ledger)
        if not decision["shouldRetry"]:
            result["finalizeRetryLedger"] = ledger
            result["terminalReason"] = decision["terminalReason"]
            return result
        seen_tuples.add(str(decision["blockerEvidenceTuple"]))
        retry_number += 1


def complete_work_block(
    repo_root_arg: Path,
    *,
    work_block_id: Optional[str] = None,
    finalize: bool = False,
    auto_approve: bool = False,
    require_repo_closed: bool = False,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="complete", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    require_repo_closed = bool(require_repo_closed or config.get("hardClean", {}).get("requireForCompletion", False))
    branch = current_branch(repo_root)
    protected_target_noop_enabled = bool(config.get("hardClean", {}).get("protectedTargetNoopCloseout", {}).get("enabled", True))
    if protected_target_noop_enabled and not work_block_id and finalize and branch and is_protected_branch(config, branch):
        selection = {
            "selectedWorkBlockId": None,
            "branch": branch,
            "worktree": str(repo_root),
            "state": "none",
            "selectionReason": "protected_target_no_active_work_block",
            "candidateCount": 0,
        }
        postcondition = verify_repo_closed_postcondition(
            repo_root,
            config,
            work_block_id=None,
            finalize_result={"status": "success", "reason": "protected_target_noop"},
        )
        result = {
            "status": "success" if postcondition["ok"] else "blocked",
            "reason": "protected_target_repo_closed" if postcondition["ok"] else "repo_closed_postcondition_failed",
            "workBlockId": None,
            "selectedWorkBlockId": None,
            "workBlockSelection": selection,
            "repoClosedPostcondition": postcondition,
            "finalizeStatus": "noop",
        }
        if not postcondition["ok"]:
            blocker_kinds = {str(item.get("kind") or "") for item in postcondition.get("blockers", []) if isinstance(item, dict)}
            if "non_exempt_dirty_files" in blocker_kinds:
                recovered = recover_dirty_protected_target_to_work_block(repo_root, config, branch)
                recovered_manifest = attach_work_block_selection(
                    recovered["manifest"],
                    "recovered_dirty_protected_target",
                    1,
                )
                result = {
                    "status": "blocked",
                    "reason": "protected_target_dirty_recovered_to_feature_branch",
                    "workBlockId": str(recovered_manifest["workBlockId"]),
                    "selectedWorkBlockId": str(recovered_manifest["workBlockId"]),
                    "workBlockSelection": recovered_manifest.get("workBlockSelection"),
                    "repoClosedPostcondition": postcondition,
                    "finalizeStatus": "recovered-to-feature-branch",
                    "recoveryBranch": str(recovered_manifest.get("branch") or ""),
                    "recoveryManifest": recovered_manifest,
                }
                write_audit(
                    repo_root,
                    config,
                    "protected-target-dirty-recovery",
                    result,
                    work_block_id=str(recovered_manifest["workBlockId"]),
                    outcome="blocked",
                )
                return result
        write_audit(
            repo_root,
            config,
            "protected-target-noop-closeout",
            result,
            work_block_id=None,
            outcome="success" if postcondition["ok"] else "blocked",
        )
        return result
    manifest = ensure_work_block_for_current_branch(repo_root, config, work_block_id)
    block_id = str(manifest["workBlockId"])
    commit_subject = normalize_commit_subject(summary)
    if summary and not commit_subject:
        raise HygieneError("work block summary must be a human-readable commit subject")
    manifest_updates: Dict[str, Any] = {"state": "completed", "completedAt": utc_now()}
    if commit_subject:
        manifest_updates["commitSubject"] = commit_subject
    update_manifest(repo_root, config, block_id, manifest_updates)
    append_event(
        repo_root,
        config,
        block_id,
        {"event": "work_block_completed", "finalizeRequested": bool(finalize), "commitSubject": commit_subject or manifest.get("commitSubject")},
    )
    detection = detect_work_block(repo_root, work_block_id=block_id)
    if not finalize:
        return {"status": "completed", "workBlockId": block_id, "workBlockSelection": manifest.get("workBlockSelection"), "detector": detection}
    result = finalize_work_block(repo_root, work_block_id=block_id, auto_approve=auto_approve, require_repo_closed=require_repo_closed)
    result["workBlockSelection"] = manifest.get("workBlockSelection")
    return result


def checkpoint_owned_dirty_action_id() -> str:
    return "checkpoint-owned-dirty"


def checkpoint_owned_dirty_candidate_id(work_block_id: str, paths: Sequence[str], detector_hash: str) -> str:
    return "candidate:checkpoint-owned-dirty:%s" % stable_hash({"workBlockId": work_block_id, "paths": sorted(paths), "detectorHash": detector_hash}, 16)


def checkpoint_owned_dirty_evidence(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any], paths: Sequence[str]) -> Dict[str, Any]:
    path_set = set(paths)
    owned_entries = [item for item in detection.get("ownedDirty", []) if item["path"] in path_set]
    manifest = load_manifest(repo_root, config, str(detection["workBlockId"]))
    return {
        "workBlockId": detection["workBlockId"],
        "branch": detection["branch"],
        "featureHead": detection["featureHead"],
        "targetHead": detection["targetHead"],
        "detectorHash": detection["detectorHash"],
        "paths": sorted(paths),
        "ownedDirty": owned_entries,
        "dirtyPathFingerprints": dirty_path_fingerprints(repo_root, owned_entries),
        "foreignDirtyRetained": detection.get("foreignDirty", []),
        "mixedDirty": detection.get("mixedDirty", []),
        "unownedDirty": detection.get("unownedDirty", []),
        "dirtyBaseline": manifest.get("dirtyBaseline"),
        "policy": {
            "allowOwnedDirtyCheckpoint": config.get("stashPolicy", {}).get("allowOwnedDirtyCheckpoint", True),
            "autoClaimCleanAtStart": config.get("dirty", {}).get("autoClaimCleanAtStart", True),
        },
    }


def plan_checkpoint_owned_dirty_candidate(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    paths = sorted({item["path"] for item in detection.get("ownedDirty", [])})
    if not paths:
        return None
    candidate_id = checkpoint_owned_dirty_candidate_id(str(detection["workBlockId"]), paths, str(detection["detectorHash"]))
    pinned_refs = {
        "feature": detection["pinnedRefs"]["feature"],
        "target": detection["pinnedRefs"]["target"],
        "dirtyPaths": paths,
    }
    evidence = checkpoint_owned_dirty_evidence(repo_root, config, detection, paths)
    return {
        "candidateId": candidate_id,
        "actionId": checkpoint_owned_dirty_action_id(),
        "actionClass": "owned_dirty_checkpoint",
        "workBlockId": detection["workBlockId"],
        "paths": paths,
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": pinned_refs,
    }


def staged_path_set(repo_root: Path) -> set[str]:
    result = run_git(repo_root, ["diff", "--cached", "--name-only", "-z", "--"], check=True)
    return set(nul_separated_paths(result.stdout))


def checkpoint_owned_work(repo_root_arg: Path, *, work_block_id: Optional[str] = None, message: str = "brokered closeout checkpoint") -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="checkpoint-owned-dirty", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    detection = detect_work_block(repo_root, work_block_id=work_block_id)
    block_id = detection["workBlockId"]
    manifest = load_manifest(repo_root, config, block_id)
    if detection.get("mixedDirty"):
        paths = [item["path"] for item in detection["mixedDirty"]]
        payload = {
            "reason": "baseline-dirty-overlaps-candidate",
            "mixedDirty": detection["mixedDirty"],
            "recoveryCommand": baseline_dirty_recovery_command(paths),
        }
        write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
        return {"status": "blocked", **payload}
    if detection["unownedDirty"]:
        payload = {"reason": "checkpoint_refuses_non_owned_dirty", "foreignDirty": detection["foreignDirty"], "unownedDirty": detection["unownedDirty"]}
        write_audit(repo_root, config, "blocked_repair", payload, work_block_id=block_id, outcome="blocked")
        return {"status": "blocked", **payload}
    if detection["foreignDirty"]:
        write_audit(repo_root, config, "retained_foreign_dirty", {"foreignDirty": detection["foreignDirty"]}, work_block_id=block_id, outcome="retained")
    candidate = plan_checkpoint_owned_dirty_candidate(repo_root, config, detection)
    if not candidate:
        return {"status": "noop", "reason": "no_owned_dirty"}
    paths = candidate["paths"]
    blockers: List[str] = []
    staged_before = staged_path_set(repo_root)
    outside_staged_before = sorted(staged_before.difference(paths))
    if outside_staged_before:
        blockers.append("staged_paths_outside_owned_dirty")
    quorum_result = ensure_autonomous_quorum(
        repo_root,
        config,
        candidate_id=candidate["candidateId"],
        action_id=candidate["actionId"],
        evidence_hash=candidate["evidenceHash"],
        pinned_refs=candidate["pinnedRefs"],
        evidence=candidate["evidence"],
        action_class=candidate["actionClass"],
        blockers=blockers,
    )
    if not quorum_result["quorum"]["ok"]:
        payload = {"candidate": candidate, "quorum": quorum_result, "blockers": blockers, "outsideStagedPaths": outside_staged_before}
        write_audit(repo_root, config, "checkpoint_owned_dirty_blocked", payload, work_block_id=block_id, outcome="blocked")
        return {"status": "blocked", "reason": quorum_result["quorum"]["reason"] or "checkpoint_owned_dirty_blocked", **payload}
    latest_detection = detect_work_block(repo_root, work_block_id=block_id)
    latest_candidate = plan_checkpoint_owned_dirty_candidate(repo_root, config, latest_detection)
    if (
        not latest_candidate
        or latest_candidate["candidateId"] != candidate["candidateId"]
        or latest_candidate["evidenceHash"] != candidate["evidenceHash"]
        or latest_candidate["pinnedRefs"] != candidate["pinnedRefs"]
        or latest_detection.get("mixedDirty")
        or latest_detection.get("unownedDirty")
    ):
        payload = {"candidate": candidate, "latestCandidate": latest_candidate, "latestDetectionHash": latest_detection.get("detectorHash")}
        write_audit(repo_root, config, "checkpoint_owned_dirty_stale_tuple", payload, work_block_id=block_id, outcome="blocked")
        return {"status": "blocked", "reason": "stale_tuple", **payload}
    paths_to_stage = sorted(set(paths).difference(staged_before))
    if paths_to_stage:
        # Exact path scoping keeps the checkpoint bounded while -A is required
        # to stage owned deletions as well as additions and modifications.
        add = run_git(repo_root, ["add", "-A", "--", *paths_to_stage])
        if add.returncode != 0:
            payload = {
                "candidate": candidate,
                "paths": paths,
                "pathsToStage": paths_to_stage,
                "stderr": add.stderr[-3000:],
            }
            write_audit(repo_root, config, "checkpoint_owned_dirty_blocked", payload, work_block_id=block_id, outcome="blocked")
            return {"status": "blocked", "reason": "git_add_failed", **payload}
    staged_after = staged_path_set(repo_root)
    outside_staged_after = sorted(staged_after.difference(paths))
    missing_staged = sorted(set(paths).difference(staged_after))
    if outside_staged_after or missing_staged:
        payload = {
            "candidate": candidate,
            "paths": paths,
            "stagedPaths": sorted(staged_after),
            "outsideStagedPaths": outside_staged_after,
            "missingStagedPaths": missing_staged,
        }
        write_audit(repo_root, config, "checkpoint_owned_dirty_blocked", payload, work_block_id=block_id, outcome="blocked")
        return {"status": "blocked", "reason": "checkpoint_stage_scope_mismatch", **payload}
    commit = run_git(
        repo_root,
        ["commit", "-m", checkpoint_commit_message(block_id, paths, explicit_message=message, work_summary=work_block_commit_subject(manifest))],
    )
    payload = {
        "candidateId": candidate["candidateId"],
        "actionId": candidate["actionId"],
        "actionClass": candidate["actionClass"],
        "evidenceHash": candidate["evidenceHash"],
        "pinnedRefs": candidate["pinnedRefs"],
        "quorum": quorum_result["quorum"],
        "paths": paths,
        "stagedPaths": sorted(staged_after.intersection(paths)),
        "returncode": commit.returncode,
        "stdout": commit.stdout[-4000:],
        "stderr": commit.stderr[-4000:],
    }
    if commit.returncode == 0:
        payload["commit"] = git_stdout(repo_root, ["rev-parse", "HEAD"])
        append_event(repo_root, config, block_id, {"event": "owned_dirty_checkpointed", "paths": paths, "commit": payload["commit"]})
    write_audit(repo_root, config, "checkpoint_owned_dirty", payload, work_block_id=block_id, outcome="success" if commit.returncode == 0 else "blocked")
    return {"status": "success" if commit.returncode == 0 else "blocked", **payload}


def quarantine_orphans(repo_root_arg: Path, *, apply: bool = False) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    if apply:
        freeze = remediation_freeze_status(repo_root, config, action="orphan-quarantine", write_audit_packet=True)
        if freeze["active"]:
            return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    root = work_blocks_root(repo_root, config)
    orphans: List[Dict[str, Any]] = []
    if root.exists():
        for manifest_file in root.glob("*/manifest.json"):
            manifest = read_json(manifest_file, {})
            worktree = Path(str(manifest.get("worktree") or ""))
            branch = str(manifest.get("branch") or "")
            branch_exists = bool(branch and rev_parse(repo_root, f"refs/heads/{branch}", required=False))
            if not worktree.exists() or not branch_exists:
                orphans.append(
                    {
                        "workBlockId": manifest.get("workBlockId") or manifest_file.parent.name,
                        "branch": branch,
                        "worktree": str(worktree),
                        "branchExists": branch_exists,
                        "worktreeExists": worktree.exists(),
                    }
                )
    payload = {"orphans": orphans, "applied": False}
    if apply:
        payload["applied"] = True
        for orphan in orphans:
            block_id = str(orphan["workBlockId"])
            try:
                update_manifest(repo_root, config, block_id, {"state": "quarantined", "quarantinedAt": utc_now()})
                append_event(repo_root, config, block_id, {"event": "orphan_quarantined", "reason": orphan})
            except HygieneError:
                pass
    write_audit(repo_root, config, "orphan_quarantine", payload, outcome="success" if apply else "recorded")
    return {"status": "success", **payload}


REPO_SWEEP_CANDIDATE_ID = "candidate:repo-sweep"
REPO_SWEEP_ACTION_ID = "repo_sweep_prune_merged"
WORKTREE_INSPECTION_SCHEMA = "worktree-inspection.v1"


def parse_worktree_porcelain(stdout: str) -> List[Dict[str, Any]]:
    worktrees: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branchRef"] = value
            prefix = "refs/heads/"
            current["branch"] = value[len(prefix) :] if value.startswith(prefix) else value
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
            current["lockReason"] = value
        elif key == "prunable":
            current["prunable"] = value or True
    if current:
        worktrees.append(current)
    return worktrees


def parse_worktree_list(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["worktree", "list", "--porcelain"], check=True)
    return parse_worktree_porcelain(result.stdout)


def worktree_inspection_state(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    result = run_git(repo_root, ["worktree", "list", "--porcelain"])
    hard_clean = config.get("hardClean", {})
    protected_patterns = list(config.get("cleanupPolicy", {}).get("protectedWorktreeRoots", []))
    state: Dict[str, Any] = {
        "schema": WORKTREE_INSPECTION_SCHEMA,
        "status": "success",
        "returncode": result.returncode,
        "stderr": result.stderr[-2000:] if result.stderr else "",
        "worktrees": [],
        "currentRootPresent": False,
        "linkedWorktreeCount": 0,
        "ordinaryLinkedWorktreeCount": 0,
        "protectedLinkedWorktreeCount": 0,
        "linkedWorktrees": [],
        "ordinaryLinkedWorktrees": [],
        "protectedLinkedWorktrees": [],
        "inspectionFailures": [],
        "requireWorktreeInspection": bool(hard_clean.get("requireWorktreeInspection", True)),
        "requireNoLinkedSiblingWorktrees": bool(hard_clean.get("requireNoLinkedSiblingWorktrees", True)),
        "protectedWorktreeRoots": protected_patterns,
    }
    if result.returncode != 0:
        state["status"] = "blocked"
        state["inspectionFailures"].append(
            {
                "kind": "worktree_list_failed",
                "returncode": result.returncode,
                "stderr": result.stderr[-2000:] if result.stderr else "",
            }
        )
        return state
    rows = parse_worktree_porcelain(result.stdout)
    state["worktrees"] = rows
    if not rows:
        state["status"] = "blocked"
        state["inspectionFailures"].append({"kind": "worktree_list_empty"})
        return state
    repo_resolved = repo_root.resolve()
    for item in rows:
        row = dict(item)
        raw_path = str(item.get("path") or "")
        path = Path(raw_path)
        rel = repo_relative_or_absolute(repo_root, path)
        row["repoRelativePath"] = rel
        try:
            resolved = path.resolve()
            row["resolvedPath"] = str(resolved)
            is_current = resolved == repo_resolved
        except OSError as exc:
            row["pathResolutionError"] = str(exc)
            is_current = False
        row["isCurrentWorktree"] = is_current
        protected = path_matches_any(rel, protected_patterns) or path_matches_any(raw_path, protected_patterns)
        row["protectedByPolicy"] = protected
        if is_current:
            state["currentRootPresent"] = True
            continue
        state["linkedWorktrees"].append(row)
        if protected:
            state["protectedLinkedWorktrees"].append(row)
        else:
            state["ordinaryLinkedWorktrees"].append(row)
    state["linkedWorktreeCount"] = len(state["linkedWorktrees"])
    state["ordinaryLinkedWorktreeCount"] = len(state["ordinaryLinkedWorktrees"])
    state["protectedLinkedWorktreeCount"] = len(state["protectedLinkedWorktrees"])
    if not state["currentRootPresent"]:
        state["inspectionFailures"].append({"kind": "current_worktree_missing", "repoRoot": str(repo_root)})
    if state["inspectionFailures"]:
        state["status"] = "blocked"
    return state


def worktree_dirty_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "dirty": False, "paths": []}
    result = run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if result.returncode != 0:
        return {"exists": True, "dirty": True, "paths": [], "statusError": result.stderr[-1000:]}
    entries = [part[3:] for part in result.stdout.split("\0") if part]
    return {"exists": True, "dirty": bool(entries), "paths": sorted(normalize_rel(path) for path in entries if normalize_rel(path))}


def safe_state_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned or stable_hash(value, 16)


def repo_sweep_reports_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return closeout_state_root(repo_root, config) / "repo-sweep" / "candidate-reports"


def repo_relative_or_absolute(repo_root: Path, path: Path) -> str:
    try:
        return normalize_rel(path.resolve().relative_to(repo_root.resolve()).as_posix())
    except ValueError:
        return str(path.resolve())


def is_protected_worktree_path(repo_root: Path, config: Dict[str, Any], path: Path) -> bool:
    rel = repo_relative_or_absolute(repo_root, path)
    return path_matches_any(rel, config.get("cleanupPolicy", {}).get("protectedWorktreeRoots", []))


def write_candidate_report(repo_root: Path, config: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    candidate_id = str(report["candidateId"])
    report_dir = repo_sweep_reports_root(repo_root, config) / safe_state_name(candidate_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "latest-report.json"
    enriched = {**report, "policyHash": config.get("policyHash"), "reportPath": str(report_path)}
    enriched["evidenceHash"] = stable_hash(
        {key: value for key, value in enriched.items() if key not in {"evidenceHash", "reportPath"}}
    )
    write_json(report_path, enriched)
    write_audit(repo_root, config, "cleanup_retention", enriched, outcome="recorded")
    return enriched


def work_block_for_branch(repo_root: Path, config: Dict[str, Any], branch: str) -> Optional[Dict[str, Any]]:
    root = work_blocks_root(repo_root, config)
    if not root.exists():
        return None
    matches: List[Dict[str, Any]] = []
    for manifest_file in root.glob("*/manifest.json"):
        manifest = read_json(manifest_file, {})
        if manifest.get("branch") == branch and manifest.get("state") in {"active", "completed", "finalizing", "blocked"}:
            matches.append(manifest)
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("updatedAt") or item.get("createdAt") or ""), reverse=True)
    return matches[0]


def classify_dirty_entries_for_branch(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    branch: str,
    branch_head: str,
    target_head: str,
    worktree_path: Path,
) -> Dict[str, Any]:
    claims = active_path_claims(repo_root, config)
    manifest = work_block_for_branch(repo_root, config, branch)
    block_id = str(manifest.get("workBlockId")) if manifest else None
    own_claims = set(manifest_path_claims(manifest)) if manifest else set()
    baseline_paths = manifest_dirty_baseline_paths(manifest) if manifest else set()
    baseline_entries = manifest_dirty_baseline_entries(manifest) if manifest else {}
    baseline_available = manifest_has_dirty_baseline(manifest) if manifest else False
    committed_delta = set(changed_paths_between(repo_root, target_head, branch_head))
    entries = parse_status_paths(worktree_path)
    owned_dirty: List[Dict[str, Any]] = []
    mixed_dirty: List[Dict[str, Any]] = []
    unowned_dirty: List[Dict[str, Any]] = []
    foreign_dirty: List[Dict[str, Any]] = []
    unclaimed_default = str(config.get("dirty", {}).get("unclaimedOutsideDelta", "foreign"))
    auto_claim_clean = bool(config.get("dirty", {}).get("autoClaimCleanAtStart", True))
    generated_patterns = config.get("paths", {}).get("generated", [])
    for entry in entries:
        path = str(entry["path"])
        owner = claims.get(path)
        sensitive = path_matches_any(path, config.get("paths", {}).get("sensitive", []))
        generated = path_matches_any(path, generated_patterns)
        in_delta = path in committed_delta
        claimed_by_self = bool(block_id and (path in own_claims or owner == block_id))
        dirty_at_baseline = path in baseline_paths
        baseline_details = dirty_baseline_change_details(worktree_path, entry, baseline_entries.get(path)) if dirty_at_baseline else {}
        baseline_changed = bool(baseline_details.get("dirtyBaselineContentChanged"))
        sensitive_auto_claim_allowed = (
            sensitive
            and auto_claim_clean
            and baseline_available
            and not dirty_at_baseline
            and clean_at_start_sensitive_auto_claim_allowed(worktree_path, config, path, entry)
        )
        enriched = {
            **entry,
            "ownerWorkBlockId": owner,
            "inCompletedBranchDelta": in_delta,
            "dirtyAtBrokerStart": dirty_at_baseline,
            "dirtyBaselineAvailable": baseline_available,
            "sensitive": sensitive,
            "generated": generated,
            "branch": branch,
            "cleanAtStartSensitiveAutoClaimAllowed": sensitive_auto_claim_allowed,
            **baseline_details,
        }
        if generated:
            enriched["classificationReason"] = "generated closeout/runtime path is not owned source work"
            foreign_dirty.append(enriched)
        elif owner and owner != block_id:
            enriched["classificationReason"] = "path is claimed by another work block"
            foreign_dirty.append(enriched)
        elif dirty_at_baseline and (baseline_changed or in_delta or claimed_by_self):
            enriched["classificationReason"] = (
                "baseline dirty path changed after broker start"
                if baseline_changed and not (in_delta or claimed_by_self)
                else "baseline dirty path overlaps branch delta or branch work-block claim"
            )
            enriched["blocker"] = "baseline-dirty-overlaps-candidate"
            enriched["recoveryCommand"] = baseline_dirty_recovery_command([path])
            mixed_dirty.append(enriched)
        elif in_delta or claimed_by_self:
            enriched["classificationReason"] = "path overlaps branch delta or branch work-block claim"
            owned_dirty.append(enriched)
        elif sensitive_auto_claim_allowed:
            enriched["classificationReason"] = "tracked sensitive note was clean or absent at broker dirty baseline"
            enriched["autoClaimedByDirtyBaseline"] = True
            owned_dirty.append(enriched)
        elif sensitive and bool(config.get("dirty", {}).get("sensitiveUnownedBlocks", True)):
            enriched["classificationReason"] = "sensitive path is unowned"
            unowned_dirty.append(enriched)
        elif unclaimed_default == "foreign":
            enriched["classificationReason"] = "path is outside completed branch delta"
            foreign_dirty.append(enriched)
        else:
            enriched["classificationReason"] = "path is unclaimed and outside branch delta"
            unowned_dirty.append(enriched)
    return {
        "workBlockId": block_id,
        "worktreePath": str(worktree_path),
        "committedDeltaPaths": sorted(committed_delta),
        "dirtyPaths": sorted({entry["path"] for entry in entries}),
        "ownedDirty": owned_dirty,
        "mixedDirty": mixed_dirty,
        "unownedDirty": unowned_dirty,
        "foreignDirty": foreign_dirty,
        "eligible": not owned_dirty and not mixed_dirty and not unowned_dirty,
    }


def extract_pid(value: str) -> Optional[int]:
    match = re.search(r"(?:pid|process)\s*[:=]\s*(\d+)", value, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def process_is_alive(pid: Optional[int]) -> Optional[bool]:
    if pid is None:
        return None
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def worktree_git_dir(path: Path) -> Optional[Path]:
    result = run_git(path, ["rev-parse", "--git-dir"])
    if result.returncode != 0:
        return None
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = path / git_dir
    return git_dir.resolve()


def inspect_worktree_lock(path: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    reason = str(item.get("lockReason") or "")
    pid = extract_pid(reason)
    git_dir = worktree_git_dir(path) if path.exists() else None
    lock_path = git_dir / "locked" if git_dir else None
    mtime: Optional[datetime] = None
    if lock_path and lock_path.exists():
        mtime = datetime.fromtimestamp(lock_path.stat().st_mtime, timezone.utc)
    age_hours = None
    if mtime:
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
    return {
        "locked": bool(item.get("locked")),
        "lockReason": reason,
        "lockPath": str(lock_path) if lock_path else None,
        "lockMtime": mtime.isoformat() if mtime else None,
        "lockAgeHours": age_hours,
        "pid": pid,
        "pidAlive": process_is_alive(pid),
    }


def is_stale_lock(lock: Dict[str, Any], config: Dict[str, Any]) -> bool:
    if not lock.get("locked"):
        return False
    pid_alive = lock.get("pidAlive")
    if pid_alive is True:
        return False
    age = lock.get("lockAgeHours")
    threshold = float(config.get("repoSweep", {}).get("lockedWorktreeStaleHours", 24))
    if age is None:
        return pid_alive is False
    return float(age) >= threshold


def blocker_auto_remediation_config(config: Dict[str, Any]) -> Dict[str, Any]:
    repo_sweep = config.get("repoSweep", {})
    retained = repo_sweep.get("retainedBlockerAutoRemediation", {})
    if isinstance(retained, dict) and not bool(retained.get("enabled", True)):
        return {}
    legacy = config.get("blockerAutoRemediation", {})
    if not bool(legacy.get("enabled", True)):
        legacy = {}
    merged = dict(legacy)
    if "allowForeignDirtyIntegratedBranchPrune" in repo_sweep:
        merged["allowForeignDirtyIntegratedBranchSwitch"] = bool(repo_sweep.get("allowForeignDirtyIntegratedBranchPrune"))
    if "recoveryBranchPrefix" in repo_sweep:
        merged["detachedDirtyBranchPrefix"] = repo_sweep.get("recoveryBranchPrefix")
    if "allowPatchEquivalentPrune" in repo_sweep:
        merged["prunePatchEquivalentBranches"] = bool(repo_sweep.get("allowPatchEquivalentPrune"))
    if "protectedLockedWorktreeExactPolicy" in repo_sweep:
        merged["explicitProtectedWorktreeActions"] = repo_sweep.get("protectedLockedWorktreeExactPolicy") or []
    return merged


def agent_remediation_config(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("agentRemediation", {})
    if not isinstance(value, dict) or not bool(value.get("enabled", True)):
        return {}
    return value


def agent_remediation_queue_config(config: Dict[str, Any]) -> Dict[str, Any]:
    legacy = agent_remediation_config(config)
    value = config.get("agentRemediationQueue", {})
    if not isinstance(value, dict):
        value = {}
    if not bool(value.get("enabled", bool(legacy.get("enabled", True)))):
        return {}
    legacy_root = legacy.get("queueRoot") or ".claude-state/closeout/agent-remediation"
    roots = value.get("queueRoots")
    if isinstance(roots, str):
        roots = [roots]
    if not isinstance(roots, list) or not roots:
        roots = [legacy_root]
    roots = [normalize_rel(str(root)) for root in roots if normalize_rel(str(root))]
    if not roots:
        roots = [".claude-state/closeout/agent-remediation"]
    return {
        "enabled": True,
        "queueRoots": roots,
        "resultRoot": normalize_rel(str(value.get("resultRoot") or ".claude-state/closeout/agent-remediation/results")),
        "maxParallelAgents": int(value.get("maxParallelAgents", 3) or 3),
        "perAgentTimeoutMs": int(value.get("perAgentTimeoutMs", 600000) or 600000),
        "maxAgentOutputBytes": int(value.get("maxAgentOutputBytes", 1048576) or 1048576),
        "requireExactTuple": bool(value.get("requireExactTuple", True)),
        "surfaceUnavailableStatus": str(value.get("surfaceUnavailableStatus") or "agent_remediation_surface_unavailable"),
    }


def config_relative_path(repo_root: Path, value: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def require_agent_queue_state_path(repo_root: Path, path: Path, *, field: str) -> Path:
    resolved = path.resolve()
    if not path_is_under(resolved, repo_root):
        raise HygieneError("%s must stay under repo root: %s" % (field, resolved))
    rel = normalize_rel(str(resolved.relative_to(repo_root.resolve()).as_posix()))
    if not (rel == ".claude-state" or rel.startswith(".claude-state/") or rel == ".codex-state" or rel.startswith(".codex-state/")):
        raise HygieneError("%s must stay under .claude-state or .codex-state: %s" % (field, rel))
    return resolved


def agent_remediation_queue_roots(repo_root: Path, config: Dict[str, Any]) -> List[Path]:
    queue_config = agent_remediation_queue_config(config)
    roots = queue_config.get("queueRoots") or [".claude-state/closeout/agent-remediation"]
    return [require_agent_queue_state_path(repo_root, config_relative_path(repo_root, str(root)), field="agentRemediationQueue.queueRoots") for root in roots]


def agent_remediation_result_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    queue_config = agent_remediation_queue_config(config)
    return require_agent_queue_state_path(
        repo_root,
        config_relative_path(repo_root, str(queue_config.get("resultRoot") or ".claude-state/closeout/agent-remediation/results")),
        field="agentRemediationQueue.resultRoot",
    )


def agent_result_packet_schema() -> Dict[str, Any]:
    return {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "requiredFields": [
            "status",
            "candidateId",
            "shardId",
            "workBlockId",
            "actionId",
            "evidenceHash",
            "policyHash",
            "pinnedRefs",
            "summary",
            "changedPaths",
            "blockers",
            "validation",
        ],
        "allowedStatuses": ["resolved", "blocked", "findings_only", "agent_remediation_surface_unavailable"],
        "mutationBoundary": "final mutation is applied only by repo-owned symbolic actors after coordinator revalidation",
    }


def agent_remediation_dirty_state_hash(repo_root: Path) -> str:
    rows: List[Dict[str, Any]] = []
    for item in parse_status_paths(repo_root):
        path = normalize_rel(str(item.get("path") or ""))
        full_path = repo_root / path
        deleted = not full_path.exists()
        rows.append(
            {
                "status": item.get("status"),
                "path": path,
                "originalPath": item.get("originalPath"),
                "deleted": deleted,
                "isDir": full_path.is_dir() if full_path.exists() else False,
                "byteSha256": None if deleted or full_path.is_dir() else file_content_hash(full_path),
            }
        )
    return stable_hash(sorted(rows, key=lambda entry: (str(entry.get("path")), str(entry.get("status")))))


def remediation_proof(config: Dict[str, Any], *, recommended_action: str, action_class: str, blockers: Sequence[str]) -> Dict[str, Any]:
    auto = blocker_auto_remediation_config(config)
    attempted = recommended_action in {
        "clean_integrate_now",
        "clean_integrate_remote_now",
        "prune_now",
        "prune_remote_now",
        "cleanup_worktree_and_prune",
        "split_now",
        "switch_target_and_prune",
        "preserve_detached_dirty_now",
        "dispatch_conflict_remediation",
    }
    excluded: List[str] = []
    if not auto:
        excluded.append("repoSweep.retainedBlockerAutoRemediation.disabled")
    excluded.extend(str(blocker) for blocker in blockers)
    if action_class == "active_locked_worktree" and "protected_worktree_root" in blockers:
        excluded.append("repoSweep.protectedLockedWorktreeExactPolicy.missing_or_mismatched")
    if action_class == "dirty_worktree" and "foreign_dirty_target_overlap" in blockers:
        excluded.append("foreign_dirty_target_overlap")
    return {
        "symbolicRemediationAttempted": attempted,
        "policyEligible": attempted or bool(excluded),
        "excludedByExactPolicy": sorted({item for item in excluded if item}),
    }


def ref_delta_paths(repo_root: Path, left_ref: str, right_ref: str) -> List[str]:
    result = run_git(repo_root, ["diff", "--name-only", "-z", f"{left_ref}..{right_ref}"])
    if result.returncode != 0:
        return []
    return sorted(set(nul_separated_paths(result.stdout)))


def merge_conflict_paths(stdout: str, stderr: str) -> List[str]:
    paths: List[str] = []
    for line in (stdout + "\n" + stderr).splitlines():
        match = re.search(r"CONFLICT\s+\([^)]+\):\s+.*?\s+in\s+(.+)$", line.strip())
        if match:
            paths.append(normalize_rel(match.group(1)))
    return sorted({path for path in paths if path})


def chunked(values: Sequence[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [list(values)]
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def conflict_group_for_path(agent_config: Dict[str, Any], path: str) -> str:
    groups = agent_config.get("conflictPathGroups", {})
    if isinstance(groups, dict):
        for name, patterns in groups.items():
            if path_matches_any(path, list(patterns or [])):
                return str(name)
    first, _, _ = normalize_rel(path).partition("/")
    return first or "root"


def agent_conflict_shards(agent_config: Dict[str, Any], conflicts: Sequence[str], changed_paths: Sequence[str]) -> List[Dict[str, Any]]:
    max_conflicts = int(agent_config.get("maxConflictFilesPerAgent", 12) or 12)
    max_changed = int(agent_config.get("maxChangedPathsPerAgent", 250) or 250)
    by_group: Dict[str, List[str]] = {}
    for path in conflicts:
        by_group.setdefault(conflict_group_for_path(agent_config, path), []).append(path)
    changed_by_group: Dict[str, List[str]] = {}
    for path in changed_paths:
        changed_by_group.setdefault(conflict_group_for_path(agent_config, path), []).append(path)
    shards: List[Dict[str, Any]] = []
    for group in sorted(by_group):
        paths = sorted(by_group[group])
        for index, path_chunk in enumerate(chunked(paths, max_conflicts), start=1):
            related = sorted(
                {
                    path
                    for path in changed_by_group.get(group, [])
                    if path in path_chunk or len(changed_by_group.get(group, [])) <= max_changed
                }
            )
            if not related:
                related = path_chunk
            shards.append(
                {
                    "shardId": "%s-%02d" % (safe_state_name(group), index),
                    "group": group,
                    "conflictPaths": path_chunk,
                    "relatedChangedPaths": related[:max_changed],
                    "instructions": "Resolve only these conflict paths in a temporary integration worktree; preserve target-side work, port feature intent, run configured validation, and report exact blockers if semantic resolution is unsafe.",
                }
            )
    return shards


def agent_conflict_resolution_packet(config: Dict[str, Any], *, candidate_id: str, branch: str, merge_probe: Dict[str, Any], changed_paths: Sequence[str]) -> Optional[Dict[str, Any]]:
    agent_config = agent_remediation_config(config)
    auto = blocker_auto_remediation_config(config)
    conflicts = list(merge_probe.get("conflicts") or [])
    legacy_max = int(auto.get("maxConflictFilesForAgent", 0) or 0)
    if merge_probe.get("reason") != "merge_failed" or not conflicts or not agent_config:
        return None
    shards = agent_conflict_shards(agent_config, conflicts, changed_paths)
    return {
        "symbolicAction": "resolve-conflicts-with-agent",
        "agentDispatch": investigation_agent_payload(config, candidate_id, "repo-sweep-conflict-remediator"),
        "branch": branch,
        "conflictPaths": conflicts,
        "changedPathCount": len(changed_paths),
        "legacyMaxConflictFilesForAgent": legacy_max,
        "shards": shards,
        "surfaceAdapters": agent_config.get("surfaceAdapters", []),
        "queueRequired": bool(agent_config.get("requireSurfaceExecution", True)),
        "recoveryCommand": closeout_script_command("remediate-retained-closeout.ps1", ["-Apply"], config),
    }


def protected_worktree_cleanup_evidence(branch: str, head: str, path: Path, lock: Dict[str, Any], target: Dict[str, Any], action: str) -> Dict[str, Any]:
    return {
        "branch": branch,
        "head": head,
        "path": str(path),
        "lockReason": lock.get("lockReason"),
        "lockPath": lock.get("lockPath"),
        "action": action,
        "target": target,
    }


def policy_paths_equivalent(left: Any, right: Any) -> bool:
    if str(left) == str(right):
        return True
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except Exception:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def explicit_protected_worktree_action(config: Dict[str, Any], evidence: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    evidence_hash = stable_hash(evidence)
    for raw in blocker_auto_remediation_config(config).get("explicitProtectedWorktreeActions", []):
        item = dict(raw or {})
        if str(item.get("branch")) != str(evidence.get("branch")):
            continue
        if not policy_paths_equivalent(item.get("path"), evidence.get("path")):
            continue
        if str(item.get("lockReason")) != str(evidence.get("lockReason")):
            continue
        if str(item.get("action")) != str(evidence.get("action")):
            continue
        if str(item.get("evidenceHash")) != evidence_hash:
            continue
        return {**item, "matchedEvidenceHash": evidence_hash}
    return None


def patch_id_for_range(repo_root: Path, left: str, right: str) -> Optional[str]:
    diff = run_git(repo_root, ["diff", "--binary", f"{left}...{right}"])
    diff_stdout = diff.stdout or ""
    if diff.returncode != 0 or not diff_stdout.strip():
        return None
    completed = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=str(repo_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        input=diff_stdout,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return stable_hash(diff.stdout)
    return completed.stdout.split()[0]


def branch_commit_subjects(repo_root: Path, left: str, right: str) -> List[str]:
    result = run_git(repo_root, ["log", "--format=%s", f"{left}..{right}"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def branch_commit_date(repo_root: Path, rev: str) -> Optional[str]:
    result = run_git(repo_root, ["log", "-1", "--format=%cI", rev])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ahead_behind_between(repo_root: Path, left: str, right: str) -> Dict[str, int]:
    result = run_git(repo_root, ["rev-list", "--left-right", "--count", f"{left}...{right}"])
    if result.returncode != 0:
        return {"behind": 0, "ahead": 0}
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return {"behind": 0, "ahead": 0}
    return {"behind": int(parts[0]), "ahead": int(parts[1])}


def backup_branch_analysis(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    branch = str(item["branch"])
    patterns = config.get("repoSweep", {}).get("backupBranchPatterns", [])
    is_backup = path_matches_any(branch, patterns)
    target_head = str(plan["pinnedRefs"]["target"]["head"])
    branch_head = str(item["head"])
    branch_tree = tree_hash(repo_root, branch_head)
    target_tree = tree_hash(repo_root, target_head)
    cherry = run_git(repo_root, ["cherry", "-v", target_head, branch_head])
    cherry_rows = [line.strip() for line in cherry.stdout.splitlines() if line.strip()] if cherry.returncode == 0 else []
    redundant_with: Optional[Dict[str, Any]] = None
    if branch_tree == target_tree or is_ancestor(repo_root, branch_head, target_head):
        redundant_with = {"kind": "target", "ref": plan["pinnedRefs"]["target"]["ref"], "head": target_head}
    elif cherry_rows and all(row.startswith("-") for row in cherry_rows):
        redundant_with = {
            "kind": "target_patch_equivalent",
            "ref": plan["pinnedRefs"]["target"]["ref"],
            "head": target_head,
            "cherry": cherry_rows,
        }
    else:
        for other in plan["branchPlans"]:
            if other["branch"] == branch:
                continue
            other_head = str(other["head"])
            if tree_hash(repo_root, other_head) == branch_tree:
                redundant_with = {"kind": "branch", "ref": other["branch"], "head": other_head}
                break
    return {
        "isBackupBranch": is_backup,
        "patterns": patterns,
        "branchTree": branch_tree,
        "targetTree": target_tree,
        "patchId": patch_id_for_range(repo_root, target_head, branch_head),
        "cherry": cherry_rows,
        "redundantWith": redundant_with,
    }


def integration_range_evidence(repo_path: Path, target_head: str, integration_head: str) -> Dict[str, Any]:
    range_spec = "%s..%s" % (target_head, integration_head)
    prefix = [
        "-c", "color.ui=false",
        "-c", "core.quotepath=false",
        "-c", "diff.external=",
        "-c", "diff.renames=false",
        "-c", "diff.context=3",
        "-c", "diff.interHunkContext=0",
        "-c", "diff.indentHeuristic=false",
    ]
    diff_check = run_git(repo_path, prefix + ["diff", "--check", "--unified=3", "--no-color", "--no-ext-diff", "--no-textconv", range_spec])
    changed = run_git(
        repo_path,
        prefix
        + ["diff-tree", "--no-commit-id", "-r", "--name-only", "-z", "--no-renames", target_head, integration_head],
        check=True,
    )
    changed_paths = sorted({normalize_rel(path) for path in changed.stdout.split("\0") if normalize_rel(path)})
    raw_identity = run_git(
        repo_path,
        prefix
        + [
            "diff-tree",
            "--no-commit-id",
            "-r",
            "--raw",
            "-z",
            "--full-index",
            "--no-renames",
            target_head,
            integration_head,
        ],
        check=True,
    )
    return {
        "range": range_spec,
        "diffCheckReturncode": diff_check.returncode,
        "diffCheckStdout": diff_check.stdout,
        "diffCheckStderr": diff_check.stderr,
        "integrationTree": tree_hash(repo_path, integration_head),
        "diffSha256": hashlib.sha256(raw_identity.stdout.encode("utf-8", errors="surrogateescape")).hexdigest(),
        "changedPaths": changed_paths,
    }


def simulate_clean_integration(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    target_head: str,
    branch_head: str,
    branch_name: str,
    target_branch: str,
) -> Dict[str, Any]:
    integration_path = closeout_state_root(repo_root, config) / "repo-sweep" / "integration-probes" / uuid.uuid4().hex[:12]
    integration_path.parent.mkdir(parents=True, exist_ok=True)
    attempt = {
        "tempWorktreePath": str(integration_path),
        "targetHead": target_head,
        "branchHead": branch_head,
    }
    add = run_git_longpaths(repo_root, ["worktree", "add", "--detach", str(integration_path), target_head])
    if add.returncode != 0:
        return {"clean": False, "reason": "probe_worktree_failed", "attempt": attempt, "returncode": add.returncode, "stderr": add.stderr[-2000:], "validationStatus": "not_reached"}
    try:
        merge = run_git(
            integration_path,
            ["merge", "--no-ff", "-m", closeout_merge_commit_message(branch_name, target_branch), branch_head],
        )
        if merge.returncode != 0:
            conflicts = merge_conflict_paths(merge.stdout, merge.stderr)
            return {
                "clean": False,
                "reason": "merge_failed",
                "attempt": attempt,
                "returncode": merge.returncode,
                "stdout": merge.stdout[-2000:],
                "stderr": merge.stderr[-2000:],
                "conflicts": conflicts,
                "validationStatus": "not_reached",
            }
        integration_head = git_stdout(integration_path, ["rev-parse", "HEAD"])
        range_evidence = integration_range_evidence(integration_path, target_head, integration_head)
        if range_evidence["diffCheckReturncode"] != 0:
            return {
                "clean": False,
                "reason": "diff_check_failed",
                "attempt": attempt,
                "integrationHead": integration_head,
                "integrationTree": range_evidence["integrationTree"],
                "range": range_evidence["range"],
                "returncode": range_evidence["diffCheckReturncode"],
                "stdout": range_evidence["diffCheckStdout"][-2000:],
                "stderr": range_evidence["diffCheckStderr"][-2000:],
                "validationStatus": "diff_check_failed",
            }
        integration_tree = range_evidence["integrationTree"]
        range_spec = range_evidence["range"]
        changed_paths = range_evidence["changedPaths"]
        diff_sha256 = range_evidence["diffSha256"]
        validations = run_validations(repo_root, config, integration_path, changed_paths=changed_paths)
        failed = next((item for item in validations if item["returncode"] != 0), None)
        if failed:
            return {
                "clean": False,
                "reason": "validation_failed",
                "attempt": attempt,
                "integrationHead": integration_head,
                "integrationTree": integration_tree,
                "range": range_spec,
                "diffSha256": diff_sha256,
                "changedPaths": changed_paths,
                "validations": validations,
                "validationStatus": "failed",
            }
        return {
            "clean": True,
            "reason": "clean_merge_and_validation_passed",
            "attempt": attempt,
            "integrationHead": integration_head,
            "integrationTree": integration_tree,
            "range": range_spec,
            "diffSha256": diff_sha256,
            "changedPaths": changed_paths,
            "validations": validations,
            "validationStatus": "passed",
        }
    finally:
        remove_worktree(repo_root, integration_path)


def local_branch_rows(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["for-each-ref", "refs/heads", "--format=%(refname:short)%1f%(objectname)%1f%(upstream:short)%1f%(subject)"], check=True)
    rows: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        while len(parts) < 4:
            parts.append("")
        rows.append({"branch": parts[0], "head": parts[1], "upstream": parts[2] or None, "subject": parts[3]})
    return rows


def stash_rows(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["stash", "list", "--format=%gd%x1f%H%x1f%gs"])
    if result.returncode != 0:
        return []
    rows: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        while len(parts) < 3:
            parts.append("")
        rows.append({"ref": parts[0], "head": parts[1], "subject": parts[2]})
    return rows


def worktree_rows(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    return parse_worktree_porcelain(result.stdout)


def repo_state_ledger_config(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("repoStateLedger", {})
    return value if isinstance(value, dict) else {}


def repo_state_dashboard_spec(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("webDashboardSpec", {})
    return value if isinstance(value, dict) else {}


def dashboard_config_value(mapping: Dict[str, Any], key: str, default: Any) -> Any:
    return mapping[key] if key in mapping else default


def dashboard_config_int(
    mapping: Dict[str, Any],
    key: str,
    default: int,
    field_name: str,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    value = dashboard_config_value(mapping, key, default)
    if isinstance(value, bool):
        raise HygieneError("%s must be an integer" % field_name)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise HygieneError("%s must be an integer" % field_name)
    if min_value is not None and parsed < min_value:
        raise HygieneError("%s must be >= %s" % (field_name, min_value))
    if max_value is not None and parsed > max_value:
        raise HygieneError("%s must be <= %s" % (field_name, max_value))
    return parsed


def rollback_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("rollbackPolicy", {})
    return value if isinstance(value, dict) else {}


def rollback_manifest_root(repo_root: Path, policy: Dict[str, Any]) -> Path:
    rel = normalize_rel(str(policy.get("manifestRoot") or ".claude-state/closeout/rollback"))
    if not rel.startswith(".claude-state/"):
        raise HygieneError("rollback manifest root must stay under .claude-state/: %s" % rel)
    root = (repo_root / rel).resolve()
    claude_state_root = (repo_root / ".claude-state").resolve()
    try:
        root.relative_to(claude_state_root)
    except ValueError as exc:
        raise HygieneError("rollback manifest root must stay under .claude-state/: %s" % rel) from exc
    return root


def require_path_under_root(repo_root: Path, rel_path: str, root: Path, field: str) -> Path:
    path = safe_repo_path(repo_root, rel_path)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HygieneError("%s must stay under %s: %s" % (field, normalize_rel(str(root.relative_to(repo_root))), normalize_rel(rel_path))) from exc
    return path


def repo_state_snapshot_evidence_hash(snapshot: Dict[str, Any]) -> str:
    hash_scope = json.loads(json.dumps(snapshot))
    state_ledger = hash_scope.get("stateLedger") if isinstance(hash_scope.get("stateLedger"), dict) else {}
    state_ledger.pop("snapshotHash", None)
    state_ledger.pop("historyPath", None)
    return stable_hash(hash_scope)


def recovery_command_forbidden_action(command: str, disallowed: set[str]) -> Optional[str]:
    text = command.lower()
    if "reset-hard" in disallowed and re.search(r"\bgit\s+reset\s+--hard\b|\breset\s+--hard\b", text):
        return "reset-hard"
    if "force-push" in disallowed and re.search(r"\bgit\s+push\b[^\r\n]*(--force|-f|\+[^\s]+)\b", text):
        return "force-push"
    if "delete-evidence" in disallowed:
        evidence_delete_patterns = [
            r"\bdelete-evidence\b",
            r"\bremove-item\b[^\r\n]*\.claude-state[\\/]+closeout",
            r"\brm\b[^\r\n]*\.claude-state[\\/]+closeout",
        ]
        if any(re.search(pattern, text) for pattern in evidence_delete_patterns):
            return "delete-evidence"
    return None


def require_integrity_checked_audit(
    repo_root: Path,
    config: Dict[str, Any],
    row: Any,
    *,
    field: str,
    audit_type: str,
    outcome: str,
) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise HygieneError("rollback %s audit evidence not found" % field)
    declared = str(row.get("auditHash") or "")
    if not declared:
        raise HygieneError("rollback %s auditHash missing" % field)
    clean = json.loads(json.dumps(row))
    clean.pop("auditHash", None)
    if stable_hash(clean) != declared:
        raise HygieneError("rollback %s audit hash mismatch" % field)
    if row.get("policyHash") != config.get("policyHash"):
        raise HygieneError("rollback %s audit policyHash mismatch" % field)
    if row.get("auditType") != audit_type or row.get("outcome") != outcome:
        raise HygieneError("rollback %s audit type/outcome mismatch" % field)
    candidates = sorted(audit_root(repo_root, config).glob("*-%s-%s.json" % (audit_type, declared[:12])))
    matching_sidecars = [path for path in candidates if read_json(path, {}).get("auditHash") == declared]
    if not matching_sidecars:
        raise HygieneError("rollback %s audit sidecar not found" % field)
    sidecar = read_json(matching_sidecars[0], {})
    if sidecar != row:
        raise HygieneError("rollback %s audit sidecar mismatch" % field)
    checked = dict(row)
    checked["auditSidecarPath"] = normalize_rel(str(matching_sidecars[0].relative_to(repo_root)))
    return checked


def repo_state_path(repo_root: Path, config: Dict[str, Any], key: str, default: str) -> Path:
    policy = repo_state_ledger_config(config)
    rel = normalize_rel(str(policy.get(key) or default))
    if not rel.startswith(".claude-state/"):
        raise HygieneError("repo state ledger path must stay under .claude-state/: %s" % rel)
    return repo_root / rel


def current_repo_ref_state(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    git_config = config.get("git", {})
    branch = current_branch(repo_root)
    head = rev_parse(repo_root, "HEAD", required=False)
    upstream = git_stdout(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], required=False) if branch else ""
    tracking = {"upstream": upstream or None, "ahead": None, "behind": None}
    if upstream:
        tracking.update(ahead_behind_between(repo_root, upstream, "HEAD"))
    target_branch = str(git_config.get("targetBranch", "master"))
    remote = str(git_config.get("remote", "origin"))
    remote_ref = f"refs/remotes/{remote}/{target_branch}"
    return {
        "currentBranch": branch,
        "head": head,
        "tracking": tracking,
        "target": {
            "branch": target_branch,
            "remote": remote if remote_exists(repo_root, remote) else None,
            "localHead": rev_parse(repo_root, f"refs/heads/{target_branch}", required=False),
            "remoteHead": rev_parse(repo_root, remote_ref, required=False),
        },
    }


def latest_closeout_audit_state(rows: Sequence[Dict[str, Any]], *, limit: int = 12) -> Dict[str, Any]:
    latest = [
        {
            "auditType": row.get("auditType"),
            "outcome": row.get("outcome"),
            "createdAt": row.get("createdAt"),
            "workBlockId": row.get("workBlockId"),
            "auditHash": row.get("auditHash"),
        }
        for row in rows[-limit:]
    ]
    latest_repo_closed = next((row for row in reversed(rows) if row.get("auditType") == "repo_closed_postcondition"), None)
    payload = latest_repo_closed.get("payload", {}) if isinstance(latest_repo_closed, dict) else {}
    truth = payload.get("closeoutCleanTruth") if isinstance(payload, dict) else None
    return {
        "auditCount": len(rows),
        "latestAudits": latest,
        "latestRepoClosedAuditHash": latest_repo_closed.get("auditHash") if isinstance(latest_repo_closed, dict) else None,
        "latestCloseoutCleanTruth": truth if isinstance(truth, dict) else None,
    }


def closeout_history_state(rows: Sequence[Dict[str, Any]], *, limit: int = 25, schema: str = "closeout-history-index.v1") -> Dict[str, Any]:
    work_blocks: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        work_block_id = row.get("workBlockId")
        if not work_block_id:
            continue
        key = str(work_block_id)
        entry = work_blocks.setdefault(
            key,
            {
                "workBlockId": key,
                "firstSeenAt": row.get("createdAt"),
                "latestSeenAt": row.get("createdAt"),
                "auditCount": 0,
                "latestAuditType": None,
                "latestOutcome": None,
                "latestAuditHash": None,
                "latestRepoClosedAuditHash": None,
                "repoClosedOk": None,
                "latestCloseoutCleanTruth": None,
            },
        )
        entry["auditCount"] = int(entry["auditCount"]) + 1
        entry["latestSeenAt"] = row.get("createdAt")
        entry["latestAuditType"] = row.get("auditType")
        entry["latestOutcome"] = row.get("outcome")
        entry["latestAuditHash"] = row.get("auditHash")
        if row.get("auditType") == "repo_closed_postcondition":
            payload = row.get("payload", {})
            if isinstance(payload, dict):
                entry["latestRepoClosedAuditHash"] = row.get("auditHash")
                entry["repoClosedOk"] = payload.get("ok")
                truth = payload.get("closeoutCleanTruth")
                entry["latestCloseoutCleanTruth"] = truth if isinstance(truth, dict) else None
    recent = list(reversed(list(work_blocks.values())))
    entries = recent[:limit]
    return {
        "schema": schema,
        "workBlockCount": len(work_blocks),
        "entryCount": len(entries),
        "entries": entries,
        "recentWorkBlocks": entries,
        "limit": limit,
        "skippedCount": max(0, len(work_blocks) - len(entries)),
        "errors": [],
    }


def rollback_readiness_state(policy: Dict[str, Any], ref_state: Dict[str, Any]) -> Dict[str, Any]:
    required_manifest_fields = list(
        policy.get("requiredManifestFields")
        or [
            "schema",
            "targetHead",
            "sourceSnapshotPath",
            "sourceSnapshotHash",
            "sourceSnapshotAuditHash",
            "repoClosedAuditHash",
            "policyHash",
            "plannedStrategy",
            "userApproval",
            "recoveryCommand",
        ]
    )
    return {
        "schema": str(policy.get("readinessSchema") or "rollback-readiness.v1"),
        "actionability": str(policy.get("readinessDefaultActionability") or "read-only-no-actor"),
        "evidenceFresh": False,
        "evidenceStatus": "not_evaluated_by_rollback_actor",
        "readinessReason": "rollback actor has not validated an immutable source snapshot and closeout-rollback-manifest.v1",
        "currentHead": ref_state.get("head"),
        "sourceSnapshotPath": None,
        "sourceSnapshotHash": None,
        "latestFeedIsRollbackEvidence": bool(policy.get("latestFeedIsRollbackEvidence", False)),
        "requiresImmutableSourceSnapshot": bool(policy.get("requireImmutableSourceSnapshotForRollback", True)),
        "requiredManifestSchema": str(policy.get("requiredManifestSchema") or "closeout-rollback-manifest.v1"),
        "requiredManifestFields": required_manifest_fields,
        "note": "latest.json is a mutable dashboard feed and cannot make rollback evidence fresh.",
    }


def validate_rollback_manifest(repo_root_arg: Path, manifest_path_arg: str | Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    policy = rollback_policy(config)
    schema = str(policy.get("requiredManifestSchema") or "closeout-rollback-manifest.v1")
    validation_schema = str(policy.get("validationSchema") or "closeout-rollback-manifest-validation.v1")
    manifest_root = rollback_manifest_root(repo_root, policy)
    manifest_path = require_path_under_root(repo_root, str(manifest_path_arg), manifest_root, "rollback manifest path")
    manifest = read_json(manifest_path, None)
    if not isinstance(manifest, dict):
        raise HygieneError("rollback manifest must be a JSON object")
    required_fields = list(policy.get("requiredManifestFields") or [])
    missing = [field for field in required_fields if field not in manifest or manifest.get(field) in (None, "")]
    if missing:
        raise HygieneError("rollback manifest missing required fields: %s" % ", ".join(missing))
    if manifest.get("schema") != schema:
        raise HygieneError("rollback manifest schema mismatch: %s" % manifest.get("schema"))
    ref_state = current_repo_ref_state(repo_root, config)
    target_head = str(manifest.get("targetHead") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", target_head):
        raise HygieneError("rollback manifest targetHead must be a 40-character commit SHA")
    if target_head != ref_state.get("head"):
        raise HygieneError("rollback manifest targetHead is stale: %s" % target_head)
    if manifest.get("policyHash") != config.get("policyHash"):
        raise HygieneError("rollback manifest policyHash mismatch")
    planned_strategy = str(manifest.get("plannedStrategy") or "")
    allowed = {str(item) for item in policy.get("allowedStrategies") or []}
    disallowed = {str(item) for item in policy.get("disallowedDefaultActions") or []}
    if planned_strategy in disallowed:
        raise HygieneError("rollback manifest plannedStrategy is forbidden: %s" % planned_strategy)
    if planned_strategy not in allowed:
        raise HygieneError("rollback manifest plannedStrategy is not allowed: %s" % planned_strategy)
    if bool(policy.get("requireUserApprovalForRollback", True)) and manifest.get("userApproval") is not True:
        raise HygieneError("rollback manifest requires explicit userApproval=true")
    recovery_command = str(manifest.get("recoveryCommand") or "").strip()
    if bool(policy.get("requireRecoveryCommandInMutatingAudits", True)) and not recovery_command:
        raise HygieneError("rollback manifest requires a recoveryCommand")
    forbidden_command_action = recovery_command_forbidden_action(recovery_command, disallowed)
    if forbidden_command_action:
        raise HygieneError("rollback manifest recoveryCommand contains forbidden action: %s" % forbidden_command_action)

    ledger = repo_state_ledger_config(config)
    latest_path = repo_state_path(repo_root, config, "latestPath", ".claude-state/closeout/repo-state/latest.json")
    history_root = repo_state_path(repo_root, config, "historyRoot", ".claude-state/closeout/repo-state/history")
    source_path_text = str(manifest.get("sourceSnapshotPath") or "")
    source_path = safe_repo_path(repo_root, source_path_text)
    if source_path.resolve() == latest_path.resolve() or source_path.name.lower() in {"latest.json", "current.json"}:
        raise HygieneError("latest/current repo-state feeds are display-only and cannot be rollback evidence")
    try:
        source_path.resolve().relative_to(history_root.resolve())
    except ValueError as exc:
        raise HygieneError("rollback sourceSnapshotPath must stay under immutable repo-state history") from exc
    source_snapshot = read_json(source_path, None)
    if not isinstance(source_snapshot, dict):
        raise HygieneError("rollback source snapshot must be a JSON object")
    if source_snapshot.get("artifactSchema") != str(ledger.get("artifactSchema") or "repo-state-snapshot.v1"):
        raise HygieneError("rollback source snapshot schema mismatch")
    if source_snapshot.get("stateLedger", {}).get("writeMode") == "latest-only":
        raise HygieneError("latest-only repo-state snapshots cannot be rollback evidence")
    declared_source_hash = str(source_snapshot.get("stateLedger", {}).get("snapshotHash") or "")
    source_hash = repo_state_snapshot_evidence_hash(source_snapshot)
    if declared_source_hash != source_hash:
        raise HygieneError("rollback source snapshot declared hash mismatch")
    if manifest.get("sourceSnapshotHash") != source_hash:
        raise HygieneError("rollback sourceSnapshotHash mismatch")
    rel_source = normalize_rel(str(source_path.relative_to(repo_root)))
    audits = audit_entries(repo_root, config)
    source_audit_hash = str(manifest.get("sourceSnapshotAuditHash") or "")
    source_audit = next((row for row in audits if row.get("auditHash") == source_audit_hash), None)
    if not isinstance(source_audit, dict):
        raise HygieneError("rollback sourceSnapshotAuditHash not found")
    source_audit = require_integrity_checked_audit(
        repo_root,
        config,
        source_audit,
        field="source snapshot",
        audit_type=str(ledger.get("auditType") or "repo_state_snapshot"),
        outcome="success",
    )
    source_audit_payload = source_audit.get("payload") if isinstance(source_audit.get("payload"), dict) else {}
    if (
        source_audit_payload.get("snapshotHash") != source_hash
        or source_audit_payload.get("historyPath") != rel_source
        or source_audit_payload.get("writeMode") != "latest-and-history"
    ):
        raise HygieneError("rollback sourceSnapshotAuditHash does not match source snapshot evidence")

    repo_closed_audit_hash = str(manifest.get("repoClosedAuditHash") or "")
    audit = next((row for row in audits if row.get("auditHash") == repo_closed_audit_hash), None)
    if not isinstance(audit, dict):
        raise HygieneError("rollback repoClosedAuditHash not found")
    audit = require_integrity_checked_audit(
        repo_root,
        config,
        audit,
        field="repoClosedAuditHash",
        audit_type="repo_closed_postcondition",
        outcome="success",
    )
    audit_payload = audit.get("payload") if isinstance(audit.get("payload"), dict) else {}
    if not audit_payload.get("ok"):
        raise HygieneError("rollback repo-closed audit did not pass")
    truth = audit_payload.get("closeoutCleanTruth") if isinstance(audit_payload.get("closeoutCleanTruth"), dict) else {}
    if truth.get("repoClosed") is not True:
        raise HygieneError("rollback repo-closed audit lacks clean closeout truth")
    audit_target = audit_payload.get("targetRef") if isinstance(audit_payload.get("targetRef"), dict) else {}
    if audit_target.get("head") and audit_target.get("head") != target_head:
        raise HygieneError("rollback repo-closed audit target head mismatch")
    source_branch = source_snapshot.get("branch") if isinstance(source_snapshot.get("branch"), dict) else {}
    if source_branch.get("head") != target_head:
        raise HygieneError("rollback source snapshot branch head mismatch")
    source_target = source_branch.get("target") if isinstance(source_branch.get("target"), dict) else {}
    source_target_heads = {str(source_target.get(key)) for key in ("localHead", "remoteHead", "head") if source_target.get(key)}
    if source_target_heads and target_head not in source_target_heads:
        raise HygieneError("rollback source snapshot target head mismatch")
    source_closeout = source_snapshot.get("closeout") if isinstance(source_snapshot.get("closeout"), dict) else {}
    if source_closeout.get("latestRepoClosedAuditHash") != repo_closed_audit_hash:
        raise HygieneError("rollback source snapshot repoClosedAuditHash binding mismatch")
    if not source_audit.get("workBlockId") or source_audit.get("workBlockId") != audit.get("workBlockId"):
        raise HygieneError("rollback source snapshot and repo-closed audits must share a workBlockId")

    rel_manifest = normalize_rel(str(manifest_path.relative_to(repo_root)))
    return {
        "schema": validation_schema,
        "status": "success",
        "actionability": str(policy.get("validatorActionability") or "read-only-validator"),
        "actionActorAvailable": False,
        "mutationReady": False,
        "mutationBoundary": "future repo-owned rollback actor only",
        "manifestPath": rel_manifest,
        "manifestHash": stable_hash(manifest),
        "targetHead": target_head,
        "currentHead": ref_state.get("head"),
        "sourceSnapshotPath": rel_source,
        "sourceSnapshotHash": source_hash,
        "sourceSnapshotHashScope": "repo-state-snapshot without stateLedger.snapshotHash/historyPath",
        "sourceSnapshotAuditHash": source_audit_hash,
        "sourceSnapshotAuditSidecarPath": source_audit.get("auditSidecarPath"),
        "repoClosedAuditHash": repo_closed_audit_hash,
        "repoClosedAuditSidecarPath": audit.get("auditSidecarPath"),
        "workBlockId": source_audit.get("workBlockId"),
        "plannedStrategy": planned_strategy,
        "latestFeedIsRollbackEvidence": False,
        "recoveryCommand": recovery_command,
    }


def repo_state_snapshot(
    repo_root_arg: Path,
    *,
    write: bool = False,
    latest_only: bool = False,
    work_block_id: Optional[str] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    from .candidate_acceptance import latest_summary as candidate_acceptance_latest_summary

    candidate_acceptance = candidate_acceptance_latest_summary(repo_root, config)
    ledger = repo_state_ledger_config(config)
    dashboard = repo_state_dashboard_spec(config)
    rollback = rollback_policy(config)
    live_refresh_command = effective_closeout_script_command(
        config,
        "write-repo-state.ps1",
        ["-Write", "-LatestOnly"],
        str(ledger.get("liveRefreshCommand") or dashboard.get("refreshCommand") or ""),
    )
    dashboard_refresh_command = effective_closeout_script_command(
        config,
        "write-repo-state.ps1",
        ["-Write", "-LatestOnly"],
        str(dashboard.get("refreshCommand") or ledger.get("liveRefreshCommand") or ""),
    )
    audit_log_rel = normalize_rel(str((audit_root(repo_root, config) / "audits.jsonl").relative_to(repo_root)))
    audit_rows = audit_entries(repo_root, config)
    state_audits = latest_closeout_audit_state(audit_rows)
    closeout_history = closeout_history_state(
        audit_rows,
        limit=int(ledger.get("closeoutHistoryLimit") or 25),
        schema=str(ledger.get("closeoutHistorySchema") or "closeout-history-index.v1"),
    )
    dirty_entries = parse_status_paths(repo_root)
    latest_path = repo_state_path(repo_root, config, "latestPath", ".claude-state/closeout/repo-state/latest.json")
    history_root = repo_state_path(repo_root, config, "historyRoot", ".claude-state/closeout/repo-state/history")
    ref_state = current_repo_ref_state(repo_root, config)
    worktree_inspection = worktree_inspection_state(repo_root, config)
    dashboard_helper = dashboard.get("helper") if isinstance(dashboard.get("helper"), dict) else {}
    dashboard_endpoints_config = dashboard.get("endpoints") if isinstance(dashboard.get("endpoints"), dict) else {}
    dashboard_endpoints_snapshot = {
        "page": str(dashboard_endpoints_config.get("page") or "/closeout"),
        "latest": str(dashboard_endpoints_config.get("latest") or "/api/closeout/repo-state/latest"),
        "historyIndex": str(dashboard_endpoints_config.get("historyIndex") or "/api/closeout/repo-state/history-index"),
        "historySnapshot": str(dashboard_endpoints_config.get("historySnapshot") or "/api/closeout/repo-state/history/{snapshotId}"),
        "actions": str(dashboard_endpoints_config.get("actions") or "/api/closeout/actions"),
        "actionsPreview": str(dashboard_endpoints_config.get("actionsPreview") or "/api/closeout/actions/preview"),
        "actionsRequest": str(dashboard_endpoints_config.get("actionsRequest") or "/api/closeout/actions/request"),
        "actionsRequestHistory": str(dashboard_endpoints_config.get("actionsRequestHistory") or "/api/closeout/actions/requests"),
        "events": str(dashboard_endpoints_config.get("events") or "/api/closeout/events"),
    }
    helper_pid_source = str(dashboard_helper.get("serverProcessIdSource") or "")
    if not helper_pid_source or helper_pid_source == "/api/closeout/actions":
        helper_pid_source = dashboard_endpoints_snapshot["actions"]
    helper_readiness_endpoint = str(dashboard_helper.get("readinessEndpoint") or "")
    if not helper_readiness_endpoint or helper_readiness_endpoint == "/api/closeout/actions":
        helper_readiness_endpoint = dashboard_endpoints_snapshot["actions"]
    dashboard_helper_snapshot = {
        "scriptPath": str(dashboard_helper.get("scriptPath") or "tools\\closeout\\start-closeout-dashboard.ps1"),
        "module": str(dashboard_helper.get("module") or "tools.repo_hygiene.closeout_dashboard"),
        "host": str(dashboard_helper.get("host") or "127.0.0.1"),
        "port": dashboard_config_int(dashboard_helper, "port", 8765, "helper.port", min_value=1, max_value=65535),
        "reuseExistingForSameRepo": bool(dashboard_helper.get("reuseExistingForSameRepo", True)),
        "serverProcessIdSource": helper_pid_source,
        "readinessEndpoint": helper_readiness_endpoint,
        "staleAfterMs": dashboard_config_int(
            dashboard_helper,
            "staleAfterMs",
            15000,
            "helper.staleAfterMs",
            min_value=DASHBOARD_MIN_INTERVAL_MS,
            max_value=DASHBOARD_MAX_INTERVAL_MS,
        ),
    }
    compare_result_path = repo_root / ".claude-state" / "closeout" / "workflow-comparison" / "compare-result.json"
    workflow_comparison: Dict[str, Any] = {
        "compareResultPath": normalize_rel(str(compare_result_path.relative_to(repo_root))),
        "compareResultAvailable": compare_result_path.exists(),
    }
    if compare_result_path.exists():
        try:
            compare_result = json.loads(compare_result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            workflow_comparison["compareResultLoadError"] = str(exc)
        else:
            if isinstance(compare_result, dict):
                workflow_comparison["compareResult"] = {
                    "artifactType": str(compare_result.get("artifactType") or ""),
                    "schemaVersion": compare_result.get("schemaVersion"),
                    "schema": str(compare_result.get("schema") or ""),
                    "status": str(compare_result.get("status") or ""),
                    "generatedAt": str(compare_result.get("generatedAt") or ""),
                    "freshnessMarkerOrTimestamp": str(compare_result.get("freshnessMarkerOrTimestamp") or ""),
                    "snapshotPointer": compare_result.get("snapshotPointer"),
                }
    snapshot = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "artifactSchema": str(ledger.get("artifactSchema") or "repo-state-snapshot.v1"),
        "artifactKind": str(ledger.get("artifactKind") or "repoStateSnapshot"),
        "status": "success",
        "capturedAt": utc_now(),
        "repo": {
            "root": str(repo_root),
            "stateRoot": normalize_rel(str(closeout_state_root(repo_root, config).relative_to(repo_root))),
            "policyHash": config.get("policyHash"),
        },
        "branch": ref_state,
        "dirty": {
            "clean": not dirty_entries,
            "entryCount": len(dirty_entries),
            "entries": dirty_entries,
        },
        "localBranches": local_branch_rows(repo_root),
        "worktrees": worktree_inspection["worktrees"],
        "worktreeInspection": worktree_inspection,
        "stashes": stash_rows(repo_root),
        "closeout": {
            "auditTrail": {
                "path": audit_log_rel,
                "auditCount": state_audits["auditCount"],
                "latestAudits": state_audits["latestAudits"],
            },
            "latestRepoClosedAuditHash": state_audits["latestRepoClosedAuditHash"],
            "latestCloseoutCleanTruth": state_audits["latestCloseoutCleanTruth"],
            "history": closeout_history,
        },
        "candidateAcceptance": candidate_acceptance,
        "dashboard": {
            "enabled": bool(dashboard.get("enabled", True)),
            "localUrl": str(dashboard.get("localUrl") or "http://127.0.0.1:8765/closeout"),
            "stickyUrlPath": str(dashboard.get("stickyUrlPath") or "/closeout"),
            "refreshTransport": str(dashboard.get("refreshTransport") or "sse-with-polling-fallback"),
            "autoRefreshMs": dashboard_config_int(
                dashboard,
                "autoRefreshMs",
                5000,
                "dashboard.autoRefreshMs",
                min_value=DASHBOARD_MIN_INTERVAL_MS,
                max_value=DASHBOARD_MAX_INTERVAL_MS,
            ),
            "refreshCommand": dashboard_refresh_command,
            "refreshCommandPolicy": str(dashboard.get("refreshCommandPolicy") or "repo-owned-write-repo-state-latest-only"),
            "liveRefreshWritesHistory": bool(ledger.get("liveRefreshWritesHistory", False)),
            "readOnlyByDefault": bool(dashboard.get("readOnlyByDefault", True)),
            "preserveClientStateAcrossRefresh": bool(dashboard.get("preserveClientStateAcrossRefresh", True)),
            "preservedClientStateKeys": list(dashboard.get("preservedClientStateKeys") or []),
            "mutationModel": str(dashboard.get("mutationModel") or "symbolic-action-request-only"),
            "feedAuthority": str(dashboard.get("feedAuthority") or "latest-json-is-display-feed-only"),
            "duplicateLaunchPolicy": str(dashboard.get("duplicateLaunchPolicy") or "reuse-same-repo-fail-foreign-owner"),
            "rollbackForbiddenActions": list(dashboard.get("rollbackForbiddenActions") or []),
            "helper": dashboard_helper_snapshot,
            "endpoints": dashboard_endpoints_snapshot,
            "workflowComparison": workflow_comparison,
            "dataSources": list(dashboard.get("dataSources") or []),
            "views": list(dashboard.get("views") or []),
            "primaryPanels": list(dashboard.get("primaryPanels") or []),
        },
        "rollback": {
            "enabled": bool(rollback.get("enabled", True)),
            "preferredStrategy": str(rollback.get("preferredStrategy") or "git-revert-or-recovery-branch"),
            "allowedStrategies": list(rollback.get("allowedStrategies") or []),
            "startNewWorkBlockForRollback": bool(rollback.get("startNewWorkBlockForRollback", True)),
            "requireUserApprovalForRollback": bool(rollback.get("requireUserApprovalForRollback", True)),
            "requireRepoStateSnapshotBeforeMutation": bool(rollback.get("requireRepoStateSnapshotBeforeMutation", True)),
            "requireRecoveryCommandInMutatingAudits": bool(rollback.get("requireRecoveryCommandInMutatingAudits", True)),
            "neverUseResetHardWithoutExplicitUserRequest": bool(rollback.get("neverUseResetHardWithoutExplicitUserRequest", True)),
            "writeRollbackPlanBeforeMutation": bool(rollback.get("writeRollbackPlanBeforeMutation", True)),
            "disallowedDefaultActions": list(rollback.get("disallowedDefaultActions") or []),
            "recoveryEvidenceRoots": list(rollback.get("recoveryEvidenceRoots") or []),
            "manifestRoot": str(rollback.get("manifestRoot") or ".claude-state/closeout/rollback"),
            "validationSchema": str(rollback.get("validationSchema") or "closeout-rollback-manifest-validation.v1"),
            "validatorCommand": str(rollback.get("validatorCommand") or ""),
            "validatorActionability": str(rollback.get("validatorActionability") or "read-only-validator"),
            "userFacingFeasibility": str(rollback.get("userFacingFeasibility") or ""),
            "readiness": rollback_readiness_state(rollback, ref_state),
        },
        "stateLedger": {
            "enabled": bool(ledger.get("enabled", True)),
            "latestPath": normalize_rel(str(latest_path.relative_to(repo_root))),
            "historyRoot": normalize_rel(str(history_root.relative_to(repo_root))),
            "historyRetentionDays": int(ledger.get("historyRetentionDays") or 90),
            "closeoutHistorySchema": str(ledger.get("closeoutHistorySchema") or "closeout-history-index.v1"),
            "liveRefreshWritesHistory": bool(ledger.get("liveRefreshWritesHistory", False)),
            "refreshCommandPolicy": str(ledger.get("refreshCommandPolicy") or "repo-owned-write-repo-state-latest-only"),
            "liveRefreshCommand": live_refresh_command,
            "feedPathPolicy": dict(ledger.get("feedPathPolicy") or {}),
        },
    }
    if write:
        snapshot["stateLedger"]["writeMode"] = "latest-only" if latest_only else "latest-and-history"
        snapshot_hash = repo_state_snapshot_evidence_hash(snapshot)
        snapshot["stateLedger"]["snapshotHash"] = snapshot_hash
        if latest_only:
            write_json(latest_path, snapshot)
        else:
            history_name = "%s-repo-state-%s.json" % (
                str(snapshot["capturedAt"]).replace(":", "").replace("+", "Z"),
                snapshot_hash[:12],
            )
            history_path = history_root / history_name
            snapshot["stateLedger"]["historyPath"] = normalize_rel(str(history_path.relative_to(repo_root)))
            write_json(latest_path, snapshot)
            write_json(history_path, snapshot)
            write_audit(
                repo_root,
                config,
                str(ledger.get("auditType") or "repo_state_snapshot"),
                {
                    "snapshotHash": snapshot_hash,
                    "latestPath": snapshot["stateLedger"]["latestPath"],
                    "historyPath": snapshot["stateLedger"]["historyPath"],
                    "writeMode": snapshot["stateLedger"]["writeMode"],
                    "branch": snapshot["branch"],
                    "dirtyEntryCount": snapshot["dirty"]["entryCount"],
                    "stashCount": len(snapshot["stashes"]),
                    "worktreeCount": len(snapshot["worktrees"]),
                    "linkedSiblingWorktreeCount": snapshot["worktreeInspection"]["ordinaryLinkedWorktreeCount"],
                    "worktreeInspectionFailureCount": len(snapshot["worktreeInspection"]["inspectionFailures"]),
                },
                work_block_id=work_block_id,
                outcome="success",
            )
    return snapshot


def remote_feature_patterns(config: Dict[str, Any]) -> List[str]:
    configured = config.get("repoSweep", {}).get("remoteFeaturePatterns", [])
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured]
    return [str(item) for item in config.get("git", {}).get("featureBranchPatterns", [])]


def remote_feature_rows(repo_root: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    git_config = config.get("git", {})
    remote = str(git_config.get("remote", "origin"))
    if not remote_exists(repo_root, remote):
        return []
    if bool(config.get("repoSweep", {}).get("fetchBeforeRemoteSweep", True)):
        run_git(repo_root, ["fetch", "--prune", remote])
    target_branch = str(git_config.get("targetBranch", "master"))
    patterns = remote_feature_patterns(config)
    result = run_git(repo_root, ["for-each-ref", f"refs/remotes/{remote}", "--format=%(refname:short)%1f%(objectname)%1f%(subject)"])
    if result.returncode != 0:
        return []
    rows: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        while len(parts) < 3:
            parts.append("")
        ref_short = parts[0]
        prefix = f"{remote}/"
        if not ref_short.startswith(prefix):
            continue
        branch = ref_short[len(prefix) :]
        if branch == "HEAD" or branch == target_branch:
            continue
        if patterns and not path_matches_any(branch, patterns):
            continue
        rows.append({"remote": remote, "branch": branch, "ref": ref_short, "head": parts[1], "subject": parts[2]})
    return rows


def cherry_rows_between(repo_root: Path, target_head: str, branch_head: str) -> List[str]:
    cherry = run_git(repo_root, ["cherry", "-v", target_head, branch_head])
    if cherry.returncode != 0:
        return []
    return [line.strip() for line in cherry.stdout.splitlines() if line.strip()]


def remote_feature_plan_for(repo_root: Path, config: Dict[str, Any], target: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    branch = str(row["branch"])
    head = str(row["head"])
    target_head = str(target["head"])
    protected = is_protected_branch(config, branch)
    ancestor = is_ancestor(repo_root, head, target_head)
    branch_tree = tree_hash(repo_root, head)
    target_tree = tree_hash(repo_root, target_head)
    tree_equal = branch_tree == target_tree
    cherry = cherry_rows_between(repo_root, target_head, head)
    patch_equivalent = bool(cherry) and all(line.startswith("-") for line in cherry)
    prune_patch_equivalent = bool(blocker_auto_remediation_config(config).get("prunePatchEquivalentBranches", True))
    disposition = "retain_remote_feature"
    reason = "remote feature requires investigation"
    if protected:
        disposition = "retain_protected_remote_feature"
        reason = "remote branch is protected by policy"
    elif ancestor or tree_equal:
        disposition = "prune_integrated_remote_feature"
        reason = "remote feature head is already integrated into target"
    elif patch_equivalent and prune_patch_equivalent:
        disposition = "prune_patch_equivalent_remote_feature"
        reason = "remote feature patch is equivalent to target"
    else:
        disposition = "merge_required_remote_feature"
        reason = "remote feature has work not proven integrated"
    return {
        **row,
        "protected": protected,
        "ancestorOfTarget": ancestor,
        "treeEqualsTarget": tree_equal,
        "branchTree": branch_tree,
        "targetTree": target_tree,
        "cherry": cherry,
        "patchEquivalentToTarget": patch_equivalent,
        "disposition": disposition,
        "reason": reason,
    }


def repo_sweep_plan(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    target = target_ref_for(repo_root, config)
    worktrees = parse_worktree_list(repo_root)
    worktree_by_branch = {item.get("branch"): item for item in worktrees if item.get("branch")}
    dirty_by_worktree_path: Dict[str, Dict[str, Any]] = {}

    def dirty_state_for(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not item:
            return {"exists": False, "dirty": False, "paths": []}
        path = str(item.get("path") or "")
        if path not in dirty_by_worktree_path:
            dirty_by_worktree_path[path] = worktree_dirty_state(Path(path))
        return dirty_by_worktree_path[path]

    branch_plans: List[Dict[str, Any]] = []
    for row in local_branch_rows(repo_root):
        branch = str(row["branch"])
        head = str(row["head"])
        worktree = worktree_by_branch.get(branch)
        protected = is_protected_branch(config, branch)
        ancestor = is_ancestor(repo_root, head, target["head"])
        dirty = dirty_state_for(worktree)
        checked_out = worktree is not None
        disposition = "retain"
        reason = "retained"
        if protected:
            disposition = "retain_protected_branch"
            reason = "protected branch"
        elif checked_out and bool(worktree.get("locked")):
            disposition = "retain_locked_worktree"
            reason = "branch is checked out in a locked worktree"
        elif checked_out and dirty.get("dirty"):
            disposition = "retain_dirty_worktree"
            reason = "branch has dirty worktree state"
        elif checked_out and ancestor:
            disposition = "retain_checked_out_merged_branch"
            reason = "branch is already integrated but checked out"
        elif ancestor:
            disposition = "prune_merged_branch"
            reason = "branch head is already ancestor of target"
        else:
            disposition = "merge_required"
            reason = "branch head is not ancestor of target"
        branch_plans.append(
            {
                **row,
                "protected": protected,
                "checkedOut": checked_out,
                "worktree": worktree,
                "worktreeDirty": dirty,
                "ancestorOfTarget": ancestor,
                "disposition": disposition,
                "reason": reason,
            }
        )
    worktree_plans: List[Dict[str, Any]] = []
    for item in worktrees:
        path = Path(str(item.get("path") or ""))
        dirty = dirty_state_for(item)
        detached = bool(item.get("detached") or not item.get("branch"))
        disposition = "retain_branch_worktree"
        reason = "branch worktree belongs to branch closeout"
        if bool(item.get("locked")):
            disposition = "retain_locked_worktree"
            reason = "worktree is locked"
        elif detached and dirty.get("dirty"):
            disposition = "retain_dirty_detached_worktree"
            reason = "detached worktree has dirty files"
        elif detached:
            disposition = "candidate_clean_detached_worktree_prune"
            reason = "detached worktree is clean"
        worktree_plans.append({**item, "dirtyState": dirty, "disposition": disposition, "reason": reason})
    stashes = stash_rows(repo_root)
    stash_plans = [
        {
            **stash,
            "disposition": "retain_stash" if config.get("repoSweep", {}).get("stashMode", "retain") == "retain" else "candidate_stash_drop",
            "reason": "stash mutation is retained unless exact tuple quorum approves a configured drop policy",
        }
        for stash in stashes
    ]
    remote_feature_plans = [remote_feature_plan_for(repo_root, config, target, row) for row in remote_feature_rows(repo_root, config)]
    pinned_refs = {
        "target": {
            "branch": target["targetBranch"],
            "remote": target.get("remote"),
            "ref": target["ref"],
            "head": target["head"],
            "mode": target["mode"],
        },
        "branches": [{"branch": item["branch"], "head": item["head"]} for item in branch_plans],
        "remoteFeatures": [{"remote": item["remote"], "branch": item["branch"], "ref": item["ref"], "head": item["head"]} for item in remote_feature_plans],
        "stashes": [{"ref": item["ref"], "head": item["head"]} for item in stash_plans],
    }
    plan = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "policyHash": config.get("policyHash"),
        "target": target,
        "branchPlans": branch_plans,
        "remoteFeaturePlans": remote_feature_plans,
        "worktreePlans": worktree_plans,
        "stashPlans": stash_plans,
        "pinnedRefs": pinned_refs,
    }
    plan["evidenceHash"] = stable_hash(plan)
    return plan


def repo_sweep_tuple(
    repo_root_arg: Path,
    *,
    plan: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    if config is None:
        config = load_closeout_config(repo_root)
    if plan is None:
        plan = repo_sweep_plan(repo_root, config)
    else:
        plan = json.loads(json.dumps(plan))
        provided_evidence_hash = str(plan.pop("evidenceHash", ""))
        expected_evidence_hash = stable_hash(plan)
        if provided_evidence_hash != expected_evidence_hash:
            raise HygieneError("repo sweep plan evidence hash is stale or invalid")
        plan["evidenceHash"] = expected_evidence_hash
    if plan.get("policyHash") != config.get("policyHash"):
        raise HygieneError("repo sweep plan policy hash does not match the supplied config")
    tuple_hash = review_tuple_hash(
        REPO_SWEEP_CANDIDATE_ID,
        REPO_SWEEP_ACTION_ID,
        plan["evidenceHash"],
        str(config.get("policyHash")),
        plan["pinnedRefs"],
    )
    return {
        "candidateId": REPO_SWEEP_CANDIDATE_ID,
        "actionId": REPO_SWEEP_ACTION_ID,
        "evidenceHash": plan["evidenceHash"],
        "policyHash": config.get("policyHash"),
        "pinnedRefs": plan["pinnedRefs"],
        "tupleHash": tuple_hash,
        "plan": plan,
    }


def branch_prune_candidate(config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    pinned_refs = {
        "target": plan["pinnedRefs"]["target"],
        "branch": {"branch": item["branch"], "head": item["head"]},
    }
    evidence = {
        "candidate": {
            "branch": item["branch"],
            "head": item["head"],
            "disposition": item["disposition"],
            "reason": item["reason"],
            "ancestorOfTarget": item["ancestorOfTarget"],
            "checkedOut": item["checkedOut"],
            "protected": item["protected"],
            "worktreeDirty": item["worktreeDirty"],
        },
        "target": plan["pinnedRefs"]["target"],
        "policy": {
            "deleteLocalBranchAfterSuccess": config.get("cleanupPolicy", {}).get("deleteLocalBranchAfterSuccess", True),
            "pruneMergedLocalBranches": config.get("repoSweep", {}).get("pruneMergedLocalBranches", True),
        },
    }
    evidence_hash = stable_hash(evidence)
    return {
        "candidateId": "candidate:branch-prune:%s" % stable_hash({"branch": item["branch"], "head": item["head"], "target": pinned_refs["target"]}, 16),
        "actionId": "delete_local_branch",
        "actionClass": "integrated_branch_prune",
        "evidence": evidence,
        "evidenceHash": evidence_hash,
        "pinnedRefs": pinned_refs,
    }


def remote_feature_action_class(item: Dict[str, Any]) -> str:
    if item.get("disposition") == "prune_patch_equivalent_remote_feature":
        return "patch_equivalent_remote_feature_prune"
    return "integrated_remote_feature_prune"


def remote_feature_prune_candidate(config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    pinned_refs = {
        "target": plan["pinnedRefs"]["target"],
        "remoteFeature": {"remote": item["remote"], "branch": item["branch"], "ref": item["ref"], "head": item["head"]},
    }
    evidence = {
        "candidate": {
            "remote": item["remote"],
            "branch": item["branch"],
            "ref": item["ref"],
            "head": item["head"],
            "disposition": item["disposition"],
            "reason": item["reason"],
            "ancestorOfTarget": item["ancestorOfTarget"],
            "treeEqualsTarget": item["treeEqualsTarget"],
            "patchEquivalentToTarget": item["patchEquivalentToTarget"],
            "cherry": item["cherry"],
            "protected": item["protected"],
        },
        "target": plan["pinnedRefs"]["target"],
        "policy": {
            "pruneRemoteFeatureBranches": config.get("repoSweep", {}).get("pruneRemoteFeatureBranches", True),
            "allowPatchEquivalentPrune": config.get("repoSweep", {}).get("allowPatchEquivalentPrune", True),
            "remoteFeaturePatterns": remote_feature_patterns(config),
        },
    }
    return {
        "candidateId": "candidate:remote-feature-prune:%s" % stable_hash({"remote": item["remote"], "branch": item["branch"], "head": item["head"], "target": pinned_refs["target"]}, 16),
        "actionId": "delete_remote_branch",
        "actionClass": remote_feature_action_class(item),
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": pinned_refs,
    }


def report_candidate_id(prefix: str, item: Dict[str, Any]) -> str:
    return "candidate:%s:%s" % (prefix, stable_hash(item, 16))


def investigation_agent_payload(config: Dict[str, Any], candidate_id: str, kind: str) -> Dict[str, Any]:
    return {
        "agentId": "agent:%s" % stable_hash({"candidateId": candidate_id, "kind": kind}, 16),
        "agentKind": kind,
        "dispatchMode": config.get("repoSweep", {}).get("agentDispatchMode", "deterministic"),
        "surface": "repo-owned-sweep",
    }


def investigate_branch_candidate(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if item.get("protected"):
        return None
    disposition = str(item.get("disposition") or "")
    backup_probe = {
        "isBackupBranch": path_matches_any(str(item["branch"]), config.get("repoSweep", {}).get("backupBranchPatterns", []))
    }
    if disposition == "prune_merged_branch" and not backup_probe["isBackupBranch"]:
        return None
    branch = str(item["branch"])
    branch_head = str(item["head"])
    target = plan["pinnedRefs"]["target"]
    target_head = str(target["head"])
    candidate_id = report_candidate_id("repo-sweep-investigate", item)
    merge_base = git_stdout(repo_root, ["merge-base", target_head, branch_head], required=False)
    scope = {
        "mergeBase": merge_base,
        "aheadBehind": ahead_behind_between(repo_root, target_head, branch_head),
        "changedPaths": changed_paths_between(repo_root, target_head, branch_head) if merge_base else [],
        "commitSubjects": branch_commit_subjects(repo_root, target_head, branch_head),
        "lastCommitDate": branch_commit_date(repo_root, branch_head),
    }
    backup = backup_branch_analysis(repo_root, config, plan, item)
    dirty_classification = None
    lock = None
    recommended_action = "retain_with_proven_blocker"
    action_class = "ambiguous_merge_required"
    blockers: List[str] = []
    recovery_command = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
    worktree = item.get("worktree") or {}
    worktree_path = Path(str(worktree.get("path") or repo_root))
    if item.get("checkedOut") and item.get("worktreeDirty", {}).get("dirty"):
        dirty_classification = classify_dirty_entries_for_branch(
            repo_root,
            config,
            branch=branch,
            branch_head=branch_head,
            target_head=target_head,
            worktree_path=worktree_path,
        )
        foreign_dirty_only = (
            bool(dirty_classification["foreignDirty"])
            and not dirty_classification["ownedDirty"]
            and not dirty_classification.get("mixedDirty")
            and not dirty_classification["unownedDirty"]
        )
        target_delta_paths = ref_delta_paths(repo_root, branch_head, target_head) if item.get("ancestorOfTarget") else []
        dirty_target_overlap = sorted(set(dirty_classification["dirtyPaths"]) & set(target_delta_paths))
        scope["targetDeltaPaths"] = target_delta_paths
        scope["dirtyTargetDeltaOverlap"] = dirty_target_overlap
        if (
            item.get("ancestorOfTarget")
            and foreign_dirty_only
            and bool(blocker_auto_remediation_config(config).get("allowForeignDirtyIntegratedBranchSwitch", True))
            and not dirty_target_overlap
            and not is_protected_worktree_path(repo_root, config, worktree_path)
        ):
            recommended_action = "switch_target_and_prune"
            action_class = "foreign_dirty_integrated_branch_prune"
            recovery_command = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
        elif dirty_classification.get("mixedDirty"):
            paths = [item["path"] for item in dirty_classification.get("mixedDirty", [])]
            action_class = "dirty_worktree"
            blockers.append("baseline-dirty-overlaps-candidate")
            recovery_command = baseline_dirty_recovery_command(paths)
        elif dirty_classification["ownedDirty"] and dirty_classification.get("workBlockId"):
            recommended_action = "split_now"
            action_class = "dirty_split"
            recovery_command = closeout_script_command("publish-checkpoint.ps1", ["-WorkBlockId", str(dirty_classification["workBlockId"])], config)
        elif dirty_classification["unownedDirty"]:
            action_class = "unowned_dirty_triage"
            blockers.append("unowned_dirty")
            recovery_command = "claim or remove unowned dirty paths, then rerun repo sweep"
        elif item.get("ancestorOfTarget") and foreign_dirty_only and dirty_target_overlap:
            action_class = "dirty_worktree"
            blockers.append("foreign_dirty_target_overlap")
            recovery_command = "foreign dirty overlaps target delta; preserve or close owning work block, then rerun repo sweep"
        else:
            action_class = "dirty_worktree"
            blockers.append("foreign_dirty_retained")
            recovery_command = "foreign dirty paths retained; rerun repo sweep after owning session closes"
    elif item.get("checkedOut") and bool(worktree.get("locked")):
        lock = inspect_worktree_lock(worktree_path, worktree)
        if is_protected_worktree_path(repo_root, config, worktree_path):
            protected_evidence = protected_worktree_cleanup_evidence(branch, branch_head, worktree_path, lock, target, "cleanup_worktree_and_prune")
            protected_request = explicit_protected_worktree_action(config, protected_evidence)
            scope["protectedWorktreeCleanupEvidence"] = {**protected_evidence, "evidenceHash": stable_hash(protected_evidence)}
            if (
                protected_request
                and not item.get("worktreeDirty", {}).get("dirty")
                and is_stale_lock(lock, config)
                and (item.get("ancestorOfTarget") or backup.get("redundantWith"))
            ):
                recommended_action = "cleanup_worktree_and_prune"
                action_class = "explicit_protected_worktree_cleanup"
                scope["explicitProtectedWorktreeAction"] = protected_request
            else:
                action_class = "active_locked_worktree"
                blockers.append("protected_worktree_root")
                recovery_command = "protected worktree cleanup requires an exact blockerAutoRemediation.explicitProtectedWorktreeActions tuple"
        elif item.get("worktreeDirty", {}).get("dirty"):
            action_class = "locked_worktree"
            blockers.append("locked_dirty_worktree")
            recovery_command = "unlock and resolve dirty paths, then rerun repo sweep"
        elif is_stale_lock(lock, config) and bool(config.get("repoSweep", {}).get("allowStaleLockedWorktreeCleanup", True)):
            if item.get("ancestorOfTarget") or (backup["isBackupBranch"] and backup.get("redundantWith")):
                recommended_action = "cleanup_worktree_and_prune"
                action_class = "stale_locked_worktree_cleanup"
            else:
                merge_probe = simulate_clean_integration(
                    repo_root,
                    config,
                    target_head=target_head,
                    branch_head=branch_head,
                    branch_name=branch,
                    target_branch=str(target["branch"]),
                )
                scope["mergeProbe"] = merge_probe
                if merge_probe.get("clean") and str(config.get("repoSweep", {}).get("mergeMode", "auto_clean")) == "auto_clean":
                    recommended_action = "clean_integrate_now"
                    action_class = "repo_sweep_clean_integrate"
                else:
                    action_class = "locked_worktree"
                    blockers.append(str(merge_probe.get("reason") or "locked_merge_required_ambiguous"))
                    recovery_command = "unlock worktree and resolve merge blockers, then rerun repo sweep"
        else:
            action_class = "active_locked_worktree"
            blockers.append("active_or_fresh_lock")
            recovery_command = "git worktree unlock %s" % worktree_path
    elif backup.get("redundantWith") and (
        backup["isBackupBranch"] or bool(blocker_auto_remediation_config(config).get("prunePatchEquivalentBranches", True))
    ):
        recommended_action = "prune_now"
        action_class = "redundant_backup_prune" if backup["isBackupBranch"] else "redundant_branch_prune"
    elif not item.get("ancestorOfTarget"):
        merge_probe = simulate_clean_integration(
            repo_root,
            config,
            target_head=target_head,
            branch_head=branch_head,
            branch_name=branch,
            target_branch=str(target["branch"]),
        )
        if merge_probe.get("clean") and str(config.get("repoSweep", {}).get("mergeMode", "auto_clean")) == "auto_clean":
            recommended_action = "clean_integrate_now"
            action_class = "repo_sweep_clean_integrate"
        else:
            blockers.append(str(merge_probe.get("reason") or "merge_required_ambiguous"))
        scope["mergeProbe"] = merge_probe
        resolution_packet = agent_conflict_resolution_packet(config, candidate_id=candidate_id, branch=branch, merge_probe=merge_probe, changed_paths=scope["changedPaths"])
        if resolution_packet:
            scope["agentResolutionPacket"] = resolution_packet
            scope["agentDispatchReason"] = list(blockers)
            blockers = []
            recommended_action = "dispatch_conflict_remediation"
            action_class = "agent_conflict_remediation"
    elif item.get("checkedOut"):
        recommended_action = "cleanup_worktree_and_prune"
        action_class = "integrated_branch_prune"
    report = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "reportType": "repo_sweep_candidate_investigation",
        "candidateId": candidate_id,
        "sourceItemHash": stable_hash(item),
        "branch": branch,
        "head": branch_head,
        "sourceDisposition": disposition,
        "reason": item.get("reason"),
        "agentDispatch": investigation_agent_payload(config, candidate_id, "repo-sweep-candidate-investigator"),
        "target": target,
        "scope": scope,
        "backupAnalysis": backup,
        "lockInspection": lock,
        "dirtyClassification": dirty_classification,
        "recommendedAction": recommended_action,
        "actionClass": action_class,
        "blockers": blockers,
        "recoveryCommand": recovery_command,
        "worktree": item.get("worktree"),
        "worktreeDirty": item.get("worktreeDirty"),
    }
    report["remediationProof"] = remediation_proof(
        config,
        recommended_action=recommended_action,
        action_class=action_class,
        blockers=blockers,
    )
    report["evidenceHash"] = stable_hash({key: value for key, value in report.items() if key != "reportPath"})
    return write_candidate_report(repo_root, config, report)


def investigate_remote_feature_candidate(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    disposition = str(item.get("disposition") or "")
    if disposition in {"prune_integrated_remote_feature", "prune_patch_equivalent_remote_feature"}:
        return None
    branch = str(item["branch"])
    branch_head = str(item["head"])
    target = plan["pinnedRefs"]["target"]
    target_head = str(target["head"])
    candidate_id = report_candidate_id("repo-sweep-remote-feature-investigate", item)
    merge_base = git_stdout(repo_root, ["merge-base", target_head, branch_head], required=False)
    scope = {
        "mergeBase": merge_base,
        "aheadBehind": ahead_behind_between(repo_root, target_head, branch_head),
        "changedPaths": changed_paths_between(repo_root, target_head, branch_head) if merge_base else [],
        "commitSubjects": branch_commit_subjects(repo_root, target_head, branch_head),
        "lastCommitDate": branch_commit_date(repo_root, branch_head),
        "cherry": item.get("cherry") or [],
        "patchId": patch_id_for_range(repo_root, target_head, branch_head),
        "treeEqualsTarget": item.get("treeEqualsTarget"),
        "patchEquivalentToTarget": item.get("patchEquivalentToTarget"),
    }
    backup = backup_branch_analysis(repo_root, config, plan, {"branch": branch, "head": branch_head})
    recommended_action = "retain_with_proven_blocker"
    action_class = "ambiguous_merge_required"
    blockers: List[str] = []
    recovery_command = closeout_script_command("remediate-retained-closeout.ps1", ["-Apply"], config)
    if item.get("protected"):
        action_class = "protected_branch"
        blockers.append("protected_remote_branch")
        recovery_command = "change closeout.config.json protectedBranches or manually inspect this remote branch"
    elif backup.get("redundantWith") and bool(blocker_auto_remediation_config(config).get("prunePatchEquivalentBranches", True)):
        recommended_action = "prune_remote_now"
        action_class = "patch_equivalent_remote_feature_prune"
    elif bool(config.get("repoSweep", {}).get("cleanIntegrateRemoteFeatureBranches", True)):
        merge_probe = simulate_clean_integration(
            repo_root,
            config,
            target_head=target_head,
            branch_head=branch_head,
            branch_name=branch,
            target_branch=str(target["branch"]),
        )
        scope["mergeProbe"] = merge_probe
        if merge_probe.get("clean") and str(config.get("repoSweep", {}).get("mergeMode", "auto_clean")) == "auto_clean":
            recommended_action = "clean_integrate_remote_now"
            action_class = "remote_feature_clean_integrate"
        else:
            blockers.append(str(merge_probe.get("reason") or "remote_merge_required_ambiguous"))
            resolution_packet = agent_conflict_resolution_packet(config, candidate_id=candidate_id, branch=branch, merge_probe=merge_probe, changed_paths=scope["changedPaths"])
            if resolution_packet:
                scope["agentResolutionPacket"] = resolution_packet
                scope["agentDispatchReason"] = list(blockers)
                blockers = []
                recommended_action = "dispatch_conflict_remediation"
                action_class = "agent_conflict_remediation"
    else:
        blockers.append("remote_feature_clean_integrate_disabled")
    report = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "reportType": "repo_sweep_remote_feature_investigation",
        "candidateId": candidate_id,
        "sourceItemHash": stable_hash(item),
        "remote": item.get("remote"),
        "remoteRef": item.get("ref"),
        "branch": branch,
        "head": branch_head,
        "sourceDisposition": disposition,
        "reason": item.get("reason"),
        "agentDispatch": investigation_agent_payload(config, candidate_id, "repo-sweep-remote-feature-investigator"),
        "target": target,
        "scope": scope,
        "backupAnalysis": backup,
        "lockInspection": None,
        "dirtyClassification": None,
        "recommendedAction": recommended_action,
        "actionClass": action_class,
        "blockers": blockers,
        "recoveryCommand": recovery_command,
        "worktree": None,
        "worktreeDirty": {"exists": False, "dirty": False, "paths": []},
    }
    report["remediationProof"] = remediation_proof(
        config,
        recommended_action=recommended_action,
        action_class=action_class,
        blockers=blockers,
    )
    report["evidenceHash"] = stable_hash({key: value for key, value in report.items() if key != "reportPath"})
    return write_candidate_report(repo_root, config, report)


def detached_dirty_preservation_branch(config: Dict[str, Any], candidate_id: str) -> str:
    prefix = normalize_rel(str(blocker_auto_remediation_config(config).get("detachedDirtyBranchPrefix") or "closeout/recovery/detached")).strip("/")
    return "%s/%s" % (prefix, stable_hash(candidate_id, 12))


def is_recovery_branch(config: Dict[str, Any], branch: str) -> bool:
    prefix = normalize_rel(str(blocker_auto_remediation_config(config).get("detachedDirtyBranchPrefix") or "closeout/recovery/detached")).strip("/")
    return normalize_rel(branch).startswith(prefix + "/")


def evidence_preserving_prune_config(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("repoSweep", {}).get("evidencePreservingPrune", {})
    if not isinstance(value, dict):
        value = {}
    return {
        "enabled": bool(value.get("enabled", True)),
        "recoveryRoot": str(value.get("recoveryRoot") or ".claude-state/closeout/manual-prune"),
        "requireBundleForNonAncestor": bool(value.get("requireBundleForNonAncestor", True)),
        "requireDirtyWorktreeBytePreservation": bool(value.get("requireDirtyWorktreeBytePreservation", True)),
        "requireReviewerVerdicts": bool(value.get("requireReviewerVerdicts", True)),
        "rerunSweepAfterPrune": bool(value.get("rerunSweepAfterPrune", True)),
    }


def evidence_preserving_prune_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    prune_config = evidence_preserving_prune_config(config)
    return require_agent_queue_state_path(
        repo_root,
        config_relative_path(repo_root, str(prune_config["recoveryRoot"])),
        field="repoSweep.evidencePreservingPrune.recoveryRoot",
    )


def prune_readiness_verdicts(config: Dict[str, Any], *, candidate_id: str, classification: str) -> List[Dict[str, Any]]:
    quorum = config.get("reviewQuorum", {})
    reviewers = list(quorum.get("selfReviewers", ["codex-self"])) + list(
        quorum.get("independentReviewers", ["ancestry-safety-reviewer", "mutation-scope-reviewer"])
    )
    score = int(quorum.get("requiredScore", 10) or 10)
    return [
        {
            "reviewer": reviewer,
            "score": score,
            "verdict": "prune-ready",
            "classification": classification,
            "candidateId": candidate_id,
            "blockers": [],
        }
        for reviewer in reviewers
    ]


def recovery_artifact_evidence_hash(payload: Dict[str, Any]) -> str:
    return stable_hash({key: value for key, value in payload.items() if key != "evidenceHash"})


def write_evidence_preserving_prune_recovery(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    candidate_id: str,
    action_class: str,
    branch: str,
    head: str,
    target_head: str,
    ref: Optional[str] = None,
    reason: str = "",
    source: str = "local-branch",
    item: Optional[Dict[str, Any]] = None,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prune_config = evidence_preserving_prune_config(config)
    if not prune_config["enabled"]:
        return {"required": False, "status": "skipped", "reason": "evidence_preserving_prune_disabled"}
    non_ancestor = not is_ancestor(repo_root, head, target_head)
    historical_value = action_class in {
        "redundant_backup_prune",
        "redundant_branch_prune",
        "patch_equivalent_remote_feature_prune",
    }
    bundle_required = bool(prune_config["requireBundleForNonAncestor"] and (non_ancestor or historical_value))
    if not bundle_required and not historical_value:
        return {"required": False, "status": "skipped", "reason": "ancestor_without_historical_recovery_requirement"}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recovery_dir = evidence_preserving_prune_root(repo_root, config) / timestamp / safe_state_name(candidate_id)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    bundle_info: Optional[Dict[str, Any]] = None
    bundle_ref = ref or (f"refs/heads/{branch}" if source == "local-branch" else branch)
    if bundle_required:
        bundle_path = recovery_dir / "recovery.bundle"
        bundle = run_git(repo_root, ["bundle", "create", str(bundle_path), bundle_ref])
        bundle_info = {
            "required": True,
            "path": repo_relative_or_absolute(repo_root, bundle_path),
            "ref": bundle_ref,
            "returncode": bundle.returncode,
            "stdout": bundle.stdout[-2000:],
            "stderr": bundle.stderr[-2000:],
            "sha256": file_content_hash(bundle_path) if bundle.returncode == 0 else None,
        }
        if bundle.returncode != 0:
            payload = {
                "status": "blocked",
                "reason": "recovery_bundle_create_failed",
                "candidateId": candidate_id,
                "branch": branch,
                "head": head,
                "targetHead": target_head,
                "bundle": bundle_info,
            }
            write_audit(repo_root, config, "manual_prune_recovery", payload, outcome="blocked")
            return payload

    artifact_path = recovery_dir / "recovery.json"
    classification = "historical-only with recovery bundle" if historical_value and non_ancestor else ("integrated" if not non_ancestor else "redundant")
    payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "artifactType": "evidence_preserving_prune_recovery",
        "status": "success",
        "candidateId": candidate_id,
        "source": source,
        "actionClass": action_class,
        "classification": classification,
        "branch": branch,
        "ref": bundle_ref,
        "branchHead": head,
        "targetHead": target_head,
        "ancestorOfTarget": not non_ancestor,
        "reason": reason,
        "recoveryRoot": repo_relative_or_absolute(repo_root, recovery_dir),
        "artifactPath": repo_relative_or_absolute(repo_root, artifact_path),
        "bundle": bundle_info,
        "reviewerVerdicts": prune_readiness_verdicts(config, candidate_id=candidate_id, classification=classification),
        "recoveryCommands": [
            "git bundle verify %s" % repo_relative_or_absolute(repo_root, bundle_path) if bundle_required else "inspect recovery artifact %s" % repo_relative_or_absolute(repo_root, artifact_path),
            "git branch closeout/recovered/%s %s" % (safe_state_name(branch), head),
            closeout_script_command("repo-sweep-closeout.ps1", config=config),
        ],
        "candidate": item,
        "candidateReport": report,
    }
    payload["evidenceHash"] = recovery_artifact_evidence_hash(payload)
    write_json(artifact_path, payload)
    verification = verify_prune_recovery_artifact(repo_root, config, payload, candidate_id=candidate_id, branch_head=head, target_head=target_head)
    if not verification["ok"]:
        blocked = {**payload, "status": "blocked", "reason": "recovery_artifact_verification_failed", "verification": verification}
        write_audit(repo_root, config, "manual_prune_recovery", blocked, outcome="blocked")
        return blocked
    write_audit(repo_root, config, "manual_prune_recovery", payload, outcome="success")
    return payload


def verify_prune_recovery_artifact(
    repo_root: Path,
    config: Dict[str, Any],
    recovery: Optional[Dict[str, Any]],
    *,
    candidate_id: str,
    branch_head: str,
    target_head: str,
) -> Dict[str, Any]:
    if not recovery or recovery.get("required") is False:
        return {"ok": True, "required": False, "reasons": []}
    reasons: List[str] = []
    root = evidence_preserving_prune_root(repo_root, config)
    path_value = str(recovery.get("artifactPath") or "")
    if not path_value:
        return {"ok": False, "required": True, "reasons": ["missing_recovery_artifact_path"]}
    path = config_relative_path(repo_root, path_value)
    if not path_is_under(path, root):
        reasons.append("recovery_artifact_outside_recovery_root")
    if not path.exists():
        reasons.append("recovery_artifact_missing")
        return {"ok": False, "required": True, "reasons": reasons, "artifactPath": path_value}
    try:
        artifact = read_json(path, {})
    except Exception as exc:
        return {"ok": False, "required": True, "reasons": [*reasons, "recovery_artifact_unreadable"], "error": str(exc), "artifactPath": path_value}
    if artifact.get("candidateId") != candidate_id:
        reasons.append("candidate_id_mismatch")
    if artifact.get("branchHead") != branch_head:
        reasons.append("branch_head_mismatch")
    if artifact.get("targetHead") != target_head:
        reasons.append("target_head_mismatch")
    if artifact.get("evidenceHash") != recovery_artifact_evidence_hash(artifact):
        reasons.append("recovery_evidence_hash_mismatch")
    prune_config = evidence_preserving_prune_config(config)
    if prune_config["requireReviewerVerdicts"]:
        verdicts = artifact.get("reviewerVerdicts")
        if not isinstance(verdicts, list) or len(verdicts) < 3:
            reasons.append("reviewer_verdicts_missing")
        elif any(int(item.get("score", 0) or 0) < int(config.get("reviewQuorum", {}).get("requiredScore", 10) or 10) or item.get("blockers") for item in verdicts if isinstance(item, dict)):
            reasons.append("reviewer_verdicts_not_unanimous")
    bundle = artifact.get("bundle") if isinstance(artifact.get("bundle"), dict) else None
    if bundle and bundle.get("required"):
        bundle_path = config_relative_path(repo_root, str(bundle.get("path") or ""))
        if not path_is_under(bundle_path, root):
            reasons.append("bundle_outside_recovery_root")
        if not bundle_path.exists():
            reasons.append("bundle_missing")
        elif bundle.get("sha256") != file_content_hash(bundle_path):
            reasons.append("bundle_hash_mismatch")
        else:
            verify = run_git(repo_root, ["bundle", "verify", str(bundle_path)])
            if verify.returncode != 0:
                reasons.append("bundle_verify_failed")
    return {"ok": not reasons, "required": True, "reasons": reasons, "artifactPath": path_value}


def attach_prune_recovery_to_candidate(candidate: Dict[str, Any], recovery: Dict[str, Any]) -> Dict[str, Any]:
    if recovery.get("required") is False:
        return candidate
    enriched = dict(candidate)
    evidence = dict(enriched.get("evidence") or {})
    evidence["recoveryArtifact"] = {
        "artifactPath": recovery.get("artifactPath"),
        "bundle": recovery.get("bundle"),
        "branchHead": recovery.get("branchHead"),
        "targetHead": recovery.get("targetHead"),
        "evidenceHash": recovery.get("evidenceHash"),
        "reviewerVerdicts": recovery.get("reviewerVerdicts"),
    }
    enriched["evidence"] = evidence
    enriched["evidenceHash"] = stable_hash(evidence)
    enriched["recoveryArtifact"] = recovery
    return enriched


def write_dirty_worktree_recovery_evidence(
    repo_root: Path,
    config: Dict[str, Any],
    *,
    worktree_path: Path,
    report: Dict[str, Any],
    entries: Sequence[Dict[str, Any]],
    preservation_branch: str,
    preservation_head: str,
) -> Dict[str, Any]:
    prune_config = evidence_preserving_prune_config(config)
    if not prune_config["enabled"] or not prune_config["requireDirtyWorktreeBytePreservation"]:
        return {"required": False, "status": "skipped", "reason": "dirty_worktree_byte_preservation_disabled"}
    candidate_id = str(report.get("candidateId") or stable_hash(str(worktree_path), 16))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recovery_dir = evidence_preserving_prune_root(repo_root, config) / timestamp / safe_state_name(candidate_id)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted({str(item.get("path")) for item in entries if item.get("path")})
    diff = run_git(worktree_path, ["diff", "--binary", "--", *paths]) if paths else subprocess.CompletedProcess([], 0, "", "")
    staged_diff = run_git(worktree_path, ["diff", "--cached", "--binary", "--", *paths]) if paths else subprocess.CompletedProcess([], 0, "", "")
    tracked_diff_path = recovery_dir / "tracked.diff"
    staged_diff_path = recovery_dir / "staged.diff"
    tracked_diff_path.write_text(diff.stdout, encoding="utf-8", errors="surrogateescape")
    staged_diff_path.write_text(staged_diff.stdout, encoding="utf-8", errors="surrogateescape")
    file_hashes = remediation_dirty_path_fingerprints(worktree_path, entries)
    untracked_bytes: List[Dict[str, Any]] = []
    copied_files: List[Dict[str, Any]] = []
    for entry in entries:
        rel_path = str(entry.get("path") or "")
        status = str(entry.get("status") or "")
        source = safe_repo_path(worktree_path, rel_path)
        if not source.exists() or source.is_dir():
            continue
        copy_root = recovery_dir / ("untracked" if status == "??" else "files")
        dest = safe_repo_path(copy_root, rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        row = {
            "path": rel_path,
            "status": status,
            "preservedPath": repo_relative_or_absolute(repo_root, dest),
            "byteSha256": file_content_hash(dest),
            "fileMode": file_mode_string(dest),
        }
        copied_files.append(row)
        if status == "??":
            untracked_bytes.append(row)
    head = git_stdout(worktree_path, ["rev-parse", "HEAD"], required=False)
    target = (report.get("target") or {})
    artifact_path = recovery_dir / "dirty-worktree-recovery.json"
    payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "artifactType": "dirty_detached_worktree_recovery",
        "status": "success",
        "candidateId": candidate_id,
        "worktreePath": str(worktree_path),
        "worktreeHead": head,
        "targetHead": target.get("head"),
        "preservationBranch": preservation_branch,
        "preservationHead": preservation_head,
        "dirtyPaths": paths,
        "statusEntries": list(entries),
        "fileHashes": file_hashes,
        "trackedDiffPath": repo_relative_or_absolute(repo_root, tracked_diff_path),
        "trackedDiffSha256": file_content_hash(tracked_diff_path),
        "stagedDiffPath": repo_relative_or_absolute(repo_root, staged_diff_path),
        "stagedDiffSha256": file_content_hash(staged_diff_path),
        "untrackedBytes": untracked_bytes,
        "copiedFiles": copied_files,
        "reviewerVerdicts": prune_readiness_verdicts(config, candidate_id=candidate_id, classification="dirty detached worktree preserved"),
        "recoveryCommands": [
            "git branch %s %s" % (preservation_branch, preservation_head),
            "git apply --binary %s" % repo_relative_or_absolute(repo_root, tracked_diff_path),
            closeout_script_command("repo-sweep-closeout.ps1", config=config),
        ],
        "artifactPath": repo_relative_or_absolute(repo_root, artifact_path),
    }
    payload["evidenceHash"] = recovery_artifact_evidence_hash(payload)
    write_json(artifact_path, payload)
    verification = verify_dirty_worktree_recovery_artifact(repo_root, config, payload, expected_paths=paths, preservation_head=preservation_head)
    if not verification["ok"]:
        blocked = {**payload, "status": "blocked", "reason": "dirty_worktree_recovery_verification_failed", "verification": verification}
        write_audit(repo_root, config, "manual_prune_recovery", blocked, outcome="blocked")
        return blocked
    write_audit(repo_root, config, "manual_prune_recovery", payload, outcome="success")
    return payload


def verify_dirty_worktree_recovery_artifact(
    repo_root: Path,
    config: Dict[str, Any],
    recovery: Optional[Dict[str, Any]],
    *,
    expected_paths: Sequence[str],
    preservation_head: str,
) -> Dict[str, Any]:
    if not recovery or recovery.get("required") is False:
        return {"ok": True, "required": False, "reasons": []}
    reasons: List[str] = []
    root = evidence_preserving_prune_root(repo_root, config)
    path = config_relative_path(repo_root, str(recovery.get("artifactPath") or ""))
    if not path_is_under(path, root):
        reasons.append("dirty_recovery_artifact_outside_recovery_root")
    if not path.exists():
        return {"ok": False, "required": True, "reasons": [*reasons, "dirty_recovery_artifact_missing"]}
    artifact = read_json(path, {})
    if artifact.get("evidenceHash") != recovery_artifact_evidence_hash(artifact):
        reasons.append("dirty_recovery_evidence_hash_mismatch")
    if artifact.get("preservationHead") != preservation_head:
        reasons.append("preservation_head_mismatch")
    if sorted(artifact.get("dirtyPaths") or []) != sorted(expected_paths):
        reasons.append("dirty_path_set_mismatch")
    tracked_diff = config_relative_path(repo_root, str(artifact.get("trackedDiffPath") or ""))
    if not tracked_diff.exists():
        reasons.append("tracked_diff_missing")
    elif artifact.get("trackedDiffSha256") != file_content_hash(tracked_diff):
        reasons.append("tracked_diff_hash_mismatch")
    for row in artifact.get("untrackedBytes") or []:
        preserved = config_relative_path(repo_root, str(row.get("preservedPath") or ""))
        if not preserved.exists():
            reasons.append("untracked_bytes_missing:%s" % row.get("path"))
        elif row.get("byteSha256") != file_content_hash(preserved):
            reasons.append("untracked_bytes_hash_mismatch:%s" % row.get("path"))
    if evidence_preserving_prune_config(config)["requireReviewerVerdicts"] and len(artifact.get("reviewerVerdicts") or []) < 3:
        reasons.append("reviewer_verdicts_missing")
    return {"ok": not reasons, "required": True, "reasons": reasons, "artifactPath": str(recovery.get("artifactPath") or "")}


def investigate_worktree_candidate(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    disposition = str(item.get("disposition") or "")
    if disposition != "retain_dirty_detached_worktree":
        return None
    path = Path(str(item.get("path") or ""))
    dirty_state = item.get("dirtyState") or worktree_dirty_state(path)
    entries = parse_status_paths(path) if path.exists() else []
    paths = sorted({str(entry["path"]) for entry in entries})
    sensitive_paths = [candidate for candidate in paths if path_matches_any(candidate, config.get("paths", {}).get("sensitive", []))]
    generated_only_proof = generated_only_detached_dirty_proof(config, entries)
    auto = blocker_auto_remediation_config(config)
    candidate_id = report_candidate_id("repo-sweep-worktree-investigate", item)
    recommended_action = "retain_with_proven_blocker"
    action_class = "dirty_detached_worktree"
    blockers: List[str] = []
    recovery_command = "preserve or remove detached dirty paths, then rerun repo sweep"
    if generated_only_proof["eligible"]:
        recommended_action = "prune_generated_detached_dirty_now"
        action_class = "generated_only_detached_worktree_prune"
        recovery_command = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
    elif len(paths) > int(auto.get("maxDetachedDirtyPaths", 25)) and not bool(auto.get("allowLargeDetachedDirtyPreservation", False)):
        blockers.append("too_many_dirty_paths")
    if sensitive_paths and not bool(auto.get("allowSensitiveDetachedDirtyPreservation", False)):
        blockers.append("sensitive_dirty_paths")
    if not bool(auto.get("allowDetachedDirtyPreservation", True)):
        blockers.append("detached_dirty_preservation_disabled")
    if not blockers and paths and recommended_action != "prune_generated_detached_dirty_now":
        recommended_action = "preserve_detached_dirty_now"
        action_class = "detached_dirty_preserve"
        recovery_command = closeout_script_command("repo-sweep-closeout.ps1", ["-Apply"], config)
    report = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "reportType": "repo_sweep_worktree_investigation",
        "candidateId": candidate_id,
        "sourceItemHash": stable_hash(item),
        "branch": None,
        "head": item.get("head"),
        "sourceDisposition": disposition,
        "reason": item.get("reason"),
        "agentDispatch": investigation_agent_payload(config, candidate_id, "repo-sweep-detached-dirty-investigator"),
        "target": plan["pinnedRefs"]["target"],
        "scope": {
            "dirtyPaths": paths,
            "sensitivePaths": sensitive_paths,
            "statusEntries": entries,
        },
        "backupAnalysis": None,
        "lockInspection": inspect_worktree_lock(path, item) if bool(item.get("locked")) else None,
        "dirtyClassification": {
            "worktreePath": str(path),
            "dirtyPaths": paths,
            "sensitivePaths": sensitive_paths,
            "entries": entries,
            "generatedOnlyProof": generated_only_proof,
            "eligible": not blockers,
        },
        "recommendedAction": recommended_action,
        "actionClass": action_class,
        "blockers": blockers,
        "recoveryCommand": recovery_command,
        "worktree": item,
        "worktreeDirty": dirty_state,
        "preservationBranch": detached_dirty_preservation_branch(config, candidate_id),
    }
    report["remediationProof"] = remediation_proof(
        config,
        recommended_action=recommended_action,
        action_class=action_class,
        blockers=blockers,
    )
    report["evidenceHash"] = stable_hash({key: value for key, value in report.items() if key != "reportPath"})
    return write_candidate_report(repo_root, config, report)


def reusable_candidate_report(
    repo_root: Path,
    config: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    candidate_id: str,
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    path = repo_sweep_reports_root(repo_root, config) / safe_state_name(candidate_id) / "latest-report.json"
    if not path.is_file():
        return None
    try:
        report = read_json(path, {})
    except Exception:
        return None
    expected_hash = stable_hash(
        {key: value for key, value in report.items() if key not in {"evidenceHash", "reportPath"}}
    )
    expected_target = plan["pinnedRefs"]["target"]
    if (
        report.get("candidateId") != candidate_id
        or report.get("sourceItemHash") != stable_hash(item)
        or report.get("head") != item.get("head")
        or report.get("branch") != item.get("branch")
        or report.get("sourceDisposition") != item.get("disposition")
        or report.get("target") != expected_target
        or report.get("policyHash") != config.get("policyHash")
        or report.get("evidenceHash") != expected_hash
    ):
        return None
    report["reportPath"] = str(path)
    return report


def investigation_jobs(config: Dict[str, Any], plan: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any], Any]]:
    jobs: List[Tuple[str, Dict[str, Any], Any]] = []
    backup_patterns = config.get("repoSweep", {}).get("backupBranchPatterns", [])
    for item in plan["branchPlans"]:
        if item.get("protected"):
            continue
        if item.get("disposition") == "prune_merged_branch" and not path_matches_any(str(item["branch"]), backup_patterns):
            continue
        jobs.append((report_candidate_id("repo-sweep-investigate", item), item, investigate_branch_candidate))
    for item in plan.get("remoteFeaturePlans", []):
        if item.get("disposition") in {"prune_integrated_remote_feature", "prune_patch_equivalent_remote_feature"}:
            continue
        jobs.append((report_candidate_id("repo-sweep-remote-feature-investigate", item), item, investigate_remote_feature_candidate))
    for item in plan["worktreePlans"]:
        if item.get("disposition") != "retain_dirty_detached_worktree":
            continue
        jobs.append((report_candidate_id("repo-sweep-worktree-investigate", item), item, investigate_worktree_candidate))
    jobs.sort(key=lambda row: row[0])
    return jobs


def fresh_investigation_report(
    repo_root: Path,
    config: Dict[str, Any],
    plan: Dict[str, Any],
    candidate_id: str,
) -> Dict[str, Any]:
    for current_id, item, investigator in investigation_jobs(config, plan):
        if current_id != candidate_id:
            continue
        report = investigator(repo_root, config, plan, item)
        if report is None or report.get("candidateId") != candidate_id:
            raise HygieneError("repo sweep investigation could not be recomputed for the selected candidate")
        return report
    raise HygieneError("repo sweep investigation candidate is absent from the current plan")


def investigation_reports(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    if not bool(config.get("repoSweep", {}).get("investigateRetainedCandidates", True)):
        return {
            "reports": [],
            "total": 0,
            "reused": 0,
            "generated": 0,
            "reusedCandidateIds": [],
            "generatedCandidateIds": [],
            "pendingCandidateIds": [],
            "complete": True,
        }
    jobs = investigation_jobs(config, plan)
    max_new = positive_int(config.get("repoSweep", {}).get("maxNewInvestigationsPerRun"), 2)
    reports: List[Dict[str, Any]] = []
    pending: List[str] = []
    reused = 0
    generated = 0
    reused_candidate_ids: List[str] = []
    generated_candidate_ids: List[str] = []
    for candidate_id, item, investigator in jobs:
        cached = reusable_candidate_report(
            repo_root,
            config,
            plan,
            candidate_id=candidate_id,
            item=item,
        )
        if cached is not None:
            reports.append(cached)
            reused += 1
            reused_candidate_ids.append(candidate_id)
            continue
        if generated >= max_new:
            pending.append(candidate_id)
            continue
        report = investigator(repo_root, config, plan, item)
        if report is not None:
            reports.append(report)
            generated += 1
            generated_candidate_ids.append(candidate_id)
    reports.sort(key=lambda item: (str(item.get("actionClass")), str(item.get("branch")), str(item.get("head"))))
    return {
        "reports": reports,
        "total": len(jobs),
        "reused": reused,
        "generated": generated,
        "reusedCandidateIds": reused_candidate_ids,
        "generatedCandidateIds": generated_candidate_ids,
        "maxNewPerRun": max_new,
        "pendingCandidateIds": pending,
        "complete": not pending,
    }


def repo_sweep_candidate_filter_matches(
    requested_candidate_id: Optional[str],
    candidate: Dict[str, Any],
    report: Optional[Dict[str, Any]] = None,
) -> bool:
    if not requested_candidate_id:
        return True
    values = {str(candidate.get("candidateId") or "")}
    pinned_branch = ((candidate.get("pinnedRefs") or {}).get("branch") or {}).get("branch")
    if pinned_branch:
        values.add(str(pinned_branch))
    remote_feature = (candidate.get("pinnedRefs") or {}).get("remoteFeature") or {}
    if remote_feature.get("ref"):
        values.add(str(remote_feature.get("ref")))
    if report:
        values.add(str(report.get("candidateId") or ""))
        if report.get("branch"):
            values.add(str(report.get("branch")))
        if report.get("remoteRef"):
            values.add(str(report.get("remoteRef")))
    return str(requested_candidate_id) in values


def candidate_from_report(config: Dict[str, Any], plan: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    action = str(report["recommendedAction"])
    if action == "clean_integrate_now":
        action_id = "clean_integrate"
    elif action == "clean_integrate_remote_now":
        action_id = "clean_integrate"
    elif action == "cleanup_worktree_and_prune":
        action_id = "worktree_cleanup"
    elif action == "split_now":
        action_id = "split"
    elif action == "switch_target_and_prune":
        action_id = "worktree_cleanup"
    elif action in {"preserve_detached_dirty_now", "prune_generated_detached_dirty_now"}:
        action_id = "orphan_quarantine"
    elif action == "prune_remote_now":
        action_id = "delete_remote_branch"
    elif action == "dispatch_conflict_remediation":
        action_id = "resolve_conflicts_with_agent"
    else:
        action_id = "delete_local_branch"
    pinned_refs = {
        "target": plan["pinnedRefs"]["target"],
        "branch": {"branch": report.get("branch"), "head": report.get("head")},
        "remoteFeature": {"remote": report.get("remote"), "branch": report.get("branch"), "ref": report.get("remoteRef"), "head": report.get("head")} if report.get("remoteRef") else None,
        "worktree": report.get("worktree"),
    }
    evidence = {
        "candidateReport": {key: value for key, value in report.items() if key != "reportPath"},
        "policy": {
            "mergeMode": config.get("repoSweep", {}).get("mergeMode"),
            "allowCleanCheckedOutIntegration": config.get("repoSweep", {}).get("allowCleanCheckedOutIntegration", True),
            "allowStaleLockedWorktreeCleanup": config.get("repoSweep", {}).get("allowStaleLockedWorktreeCleanup", True),
            "retainedBlockerAutoRemediation": config.get("repoSweep", {}).get("retainedBlockerAutoRemediation"),
            "allowForeignDirtyIntegratedBranchPrune": config.get("repoSweep", {}).get("allowForeignDirtyIntegratedBranchPrune"),
            "allowPatchEquivalentPrune": config.get("repoSweep", {}).get("allowPatchEquivalentPrune"),
            "protectedLockedWorktreeExactPolicy": config.get("repoSweep", {}).get("protectedLockedWorktreeExactPolicy"),
            "pruneRemoteFeatureBranches": config.get("repoSweep", {}).get("pruneRemoteFeatureBranches"),
            "cleanIntegrateRemoteFeatureBranches": config.get("repoSweep", {}).get("cleanIntegrateRemoteFeatureBranches"),
            "deleteRemoteFeatureAfterCleanIntegrate": config.get("repoSweep", {}).get("deleteRemoteFeatureAfterCleanIntegrate"),
            "evidencePreservingPrune": config.get("repoSweep", {}).get("evidencePreservingPrune"),
        },
    }
    return {
        "candidateId": "candidate:repo-sweep-action:%s" % stable_hash({"report": report["candidateId"], "action": action}, 16),
        "actionId": action_id,
        "actionClass": report["actionClass"],
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": pinned_refs,
        "reportPath": report.get("reportPath"),
    }


def agent_remediation_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    roots = agent_remediation_queue_roots(repo_root, config)
    return roots[0] if roots else config_relative_path(repo_root, ".claude-state/closeout/agent-remediation")


def dispatch_agent_conflict_remediation(
    repo_root: Path,
    config: Dict[str, Any],
    plan: Dict[str, Any],
    report: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    packet = dict(report.get("scope", {}).get("agentResolutionPacket") or {})
    if not packet:
        return {"action": "agent_conflict_remediation_dispatch", "status": "blocked", "reason": "missing_agent_resolution_packet", "branch": report.get("branch")}
    queue_config = agent_remediation_queue_config(config)
    if not queue_config:
        return {"action": "agent_conflict_remediation_dispatch", "status": "blocked", "reason": "agent_remediation_queue_disabled", "branch": report.get("branch")}
    queue_root = agent_remediation_root(repo_root, config)
    result_root = agent_remediation_result_root(repo_root, config)
    queue_dir = queue_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / ("%s.json" % safe_state_name(str(candidate["candidateId"])))
    work_block_id = report.get("workBlockId") or report.get("scope", {}).get("workBlockId")
    candidate_report_hash = stable_hash({key: value for key, value in report.items() if key != "reportPath"})
    manifest_hash = current_manifest_hash(repo_root, config, work_block_id)
    exact_tuple = {
        "candidateId": candidate["candidateId"],
        "actionId": candidate["actionId"],
        "evidenceHash": candidate["evidenceHash"],
        "policyHash": config.get("policyHash"),
        "pinnedRefs": candidate["pinnedRefs"],
    }
    expected_schema = agent_result_packet_schema()
    validation_requirements = config.get("validation", {}).get("commands", [])
    enriched_shards: List[Dict[str, Any]] = []
    for raw_shard in packet.get("shards", []):
        shard = dict(raw_shard)
        shard_id = str(shard.get("shardId") or "shard")
        result_path = result_root / safe_state_name(str(candidate["candidateId"])) / ("%s.json" % safe_state_name(shard_id))
        allowed_read = sorted(
            {
                normalize_rel(str(path))
                for path in [*(shard.get("relatedChangedPaths") or []), *(shard.get("conflictPaths") or []), str(report.get("reportPath") or "")]
                if normalize_rel(str(path))
            }
        )
        allowed_write = sorted({normalize_rel(str(path)) for path in shard.get("conflictPaths", []) if normalize_rel(str(path))})
        shard.update(
            {
                "candidateId": candidate["candidateId"],
                "workBlockId": work_block_id,
                "actionId": candidate["actionId"],
                "evidenceHash": candidate["evidenceHash"],
                "policyHash": config.get("policyHash"),
                "pinnedRefs": candidate["pinnedRefs"],
                "allowedReadScope": allowed_read,
                "allowedWriteScope": allowed_write,
                "expectedOutputSchema": expected_schema,
                "validationRequirements": validation_requirements,
                "resultPath": repo_relative_or_absolute(repo_root, result_path),
                "recoveryCommand": packet.get("recoveryCommand"),
                "perAgentTimeoutMs": queue_config["perAgentTimeoutMs"],
                "maxAgentOutputBytes": queue_config["maxAgentOutputBytes"],
                "mutationBoundary": "repo-owned symbolic actors only",
            }
        )
        enriched_shards.append(shard)
    payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "status": "queued",
        "createdAt": utc_now(),
        "candidateId": candidate["candidateId"],
        "workBlockId": work_block_id,
        "actionId": candidate["actionId"],
        "actionClass": candidate["actionClass"],
        "evidenceHash": candidate["evidenceHash"],
        "policyHash": config.get("policyHash"),
        "pinnedRefs": candidate["pinnedRefs"],
        "exactTuple": exact_tuple,
        "requireExactTuple": queue_config["requireExactTuple"],
        "dirtyStateHash": agent_remediation_dirty_state_hash(repo_root),
        "candidateReportHash": candidate_report_hash,
        "manifestSelectionHash": manifest_hash,
        "queueRoot": repo_relative_or_absolute(repo_root, queue_root),
        "resultRoot": repo_relative_or_absolute(repo_root, result_root),
        "surfaceUnavailableStatus": queue_config["surfaceUnavailableStatus"],
        "maxParallelAgents": queue_config["maxParallelAgents"],
        "perAgentTimeoutMs": queue_config["perAgentTimeoutMs"],
        "maxAgentOutputBytes": queue_config["maxAgentOutputBytes"],
        "reportPath": report.get("reportPath"),
        "branch": report.get("branch"),
        "head": report.get("head"),
        "target": plan.get("pinnedRefs", {}).get("target"),
        "conflictPaths": packet.get("conflictPaths", []),
        "changedPathCount": packet.get("changedPathCount"),
        "shards": enriched_shards,
        "surfaceAdapters": packet.get("surfaceAdapters", []),
        "validationRequirements": validation_requirements,
        "expectedOutputSchema": expected_schema,
        "recoveryCommand": packet.get("recoveryCommand"),
        "instructions": {
            "coordinator": "Spawn one background agent per shard where available. Each agent must work in a temporary integration worktree, resolve only its assigned paths, avoid broad cleanup, report blockers with hunks, and leave mutation to repo-owned finalize actors.",
            "afterAgents": "Coordinator applies resolved shard patches one at a time in a clean target worktree, runs diff-check and configured validation, then reruns repo sweep/finalize.",
            "manualFallback": "If no surface adapter can spawn agents, use this packet as the exact recovery brief and retain the candidate with this queue artifact.",
        },
    }
    payload["dispatchHash"] = stable_hash(payload)
    write_json(queue_path, payload)
    action = {
        "action": "agent_conflict_remediation_dispatch",
        "status": "queued",
        "queuePath": str(queue_path),
        "candidateId": candidate["candidateId"],
        "branch": report.get("branch"),
        "shardCount": len(payload["shards"]),
        "conflictPathCount": len(payload["conflictPaths"]),
    }
    write_audit(repo_root, config, "agent_remediation_dispatch", {"queuePacket": payload, "action": action}, outcome="success")
    return action


def pending_agent_remediation_queue_packets(repo_root: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    packets: List[Dict[str, Any]] = []
    for root in agent_remediation_queue_roots(repo_root, config):
        queue_dir = root / "queue"
        if not queue_dir.exists():
            continue
        for path in sorted(queue_dir.glob("*.json")):
            try:
                packet = read_json(path, {})
            except Exception as exc:
                packet = {"status": "unreadable", "error": str(exc), "schemaVersion": BROKER_SCHEMA_VERSION}
            status = str(packet.get("status", "queued"))
            packet["queuePath"] = str(path)
            if status in {"consumed", "retired", "superseded"}:
                if agent_queue_retirement_proof_valid(repo_root, config, packet):
                    continue
                if reap_stale_retired_agent_queue_packet(repo_root, config, packet):
                    continue
            packets.append(packet)
    return packets


def agent_queue_retirement_proof_valid(repo_root: Path, config: Dict[str, Any], packet: Dict[str, Any], *, policy_hash: Optional[str] = None) -> bool:
    proof = packet.get("retirementProof")
    if not isinstance(proof, dict):
        return False
    exact_tuple = packet.get("exactTuple")
    if not isinstance(exact_tuple, dict):
        return False
    required_policy_hash = config.get("policyHash") if policy_hash is None else policy_hash
    required_equal = {
        "candidateId": packet.get("candidateId"),
        "actionId": packet.get("actionId"),
        "evidenceHash": packet.get("evidenceHash"),
        "policyHash": required_policy_hash,
    }
    for key, expected in required_equal.items():
        if proof.get(key) != expected:
            return False
    if proof.get("exactTuple") != exact_tuple:
        return False
    if proof.get("pinnedRefs") != packet.get("pinnedRefs"):
        return False
    if proof.get("resultCollectionStatus") != "success":
        return False
    if not proof.get("resultCollectionHash"):
        return False
    path_value = proof.get("resultCollectionPath")
    if not path_value:
        return False
    try:
        path = resolve_agent_result_path(repo_root, config, str(path_value))
    except HygieneError:
        return False
    if not path.exists():
        return False
    try:
        artifact = read_json(path, {})
    except Exception:
        return False
    if artifact.get("resultCollectionHash") != proof.get("resultCollectionHash"):
        return False
    actual_hash = stable_hash({key: value for key, value in artifact.items() if key not in {"resultCollectionHash", "resultCollectionPath"}})
    if actual_hash != proof.get("resultCollectionHash"):
        return False
    if artifact.get("status") != "success":
        return False
    collected_for_packet = [
        item
        for item in artifact.get("collectedResults", [])
        if isinstance(item, dict) and str(item.get("candidateId")) == str(packet.get("candidateId"))
    ]
    if not collected_for_packet:
        return False
    for item in collected_for_packet:
        if item.get("actionId") != packet.get("actionId"):
            return False
        if item.get("evidenceHash") != packet.get("evidenceHash"):
            return False
        if item.get("policyHash") != required_policy_hash:
            return False
        if item.get("pinnedRefs") != packet.get("pinnedRefs"):
            return False
        if item.get("exactTuple") != exact_tuple:
            return False
    if proof.get("retiredPacketStatus") not in {"consumed", "retired", "superseded"}:
        return False
    return True


def stale_retired_agent_queue_reap_evidence(repo_root: Path, config: Dict[str, Any], packet: Dict[str, Any]) -> Dict[str, Any]:
    status = str(packet.get("status", "queued"))
    historical_policy_hash = packet.get("policyHash")
    current_policy_hash = config.get("policyHash")
    reasons: List[Dict[str, Any]] = []
    pinned_validations: List[Dict[str, Any]] = []
    if status not in {"consumed", "retired", "superseded"}:
        reasons.append({"kind": "packet_not_retired", "status": status})
    if not agent_queue_retirement_proof_valid(repo_root, config, packet, policy_hash=historical_policy_hash):
        reasons.append({"kind": "historical_retirement_proof_invalid"})
    for row in pinned_ref_expected_actual(repo_root, "pinnedRefs", packet.get("pinnedRefs")):
        validation = dict(row)
        if row["expected"] == row["actual"]:
            validation["status"] = "current"
        elif row["label"] == "pinnedRefs.target" and row["actual"] and is_ancestor(repo_root, str(row["expected"]), str(row["actual"])):
            validation["status"] = "target_descends_from_retired_tuple"
        elif row["label"] in {"pinnedRefs.branch", "pinnedRefs.worktree"} and row["actual"] is None:
            validation["status"] = "retired_ref_absent"
        else:
            validation["status"] = "blocked"
            reasons.append({"kind": "pinned_ref_not_reapable", **row})
        pinned_validations.append(validation)
    evidence_payload = {
        "candidateId": packet.get("candidateId"),
        "actionId": packet.get("actionId"),
        "originalEvidenceHash": packet.get("evidenceHash"),
        "historicalPolicyHash": historical_policy_hash,
        "currentPolicyHash": current_policy_hash,
        "historicalExactTuple": packet.get("exactTuple"),
        "historicalPinnedRefs": packet.get("pinnedRefs"),
        "pinnedRefValidation": pinned_validations,
        "retiredPacketStatus": status,
        "retirementProofHash": stable_hash(packet.get("retirementProof")),
    }
    reap_evidence_hash = stable_hash(evidence_payload)
    reap_exact_tuple = {
        "candidateId": packet.get("candidateId"),
        "actionId": "reap_stale_retired_agent_queue_packet",
        "evidenceHash": reap_evidence_hash,
        "policyHash": current_policy_hash,
        "pinnedRefs": {"historical": packet.get("pinnedRefs"), "validation": pinned_validations},
    }
    return {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "status": "success" if not reasons else "blocked",
        "reapReason": "stale_retired_agent_queue_packet",
        "candidateId": packet.get("candidateId"),
        "workBlockId": packet.get("workBlockId"),
        "queuePath": packet.get("queuePath"),
        "originalActionId": packet.get("actionId"),
        "currentPolicyHash": current_policy_hash,
        "historicalPolicyHash": historical_policy_hash,
        "historicalExactTuple": packet.get("exactTuple"),
        "reapExactTuple": reap_exact_tuple,
        "reapEvidenceHash": reap_evidence_hash,
        "pinnedRefValidation": pinned_validations,
        "blockers": reasons,
    }


def reap_stale_retired_agent_queue_packet(repo_root: Path, config: Dict[str, Any], packet: Dict[str, Any]) -> bool:
    evidence = stale_retired_agent_queue_reap_evidence(repo_root, config, packet)
    if evidence["status"] != "success":
        return False
    write_audit(repo_root, config, "agent_remediation_stale_retired_packet_reap", evidence, work_block_id=packet.get("workBlockId"), outcome="success")
    return True


def refresh_agent_queue_remote_refs(repo_root: Path, config: Dict[str, Any], packets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remotes: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            remote = value.get("remote")
            if remote:
                remotes.add(str(remote))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for packet in packets:
        visit(packet.get("pinnedRefs"))
    failures: List[Dict[str, Any]] = []
    for remote in sorted(remotes):
        if not remote_exists(repo_root, remote):
            failures.append({"kind": "remote_fetch_failed", "remote": remote, "reason": "remote_missing"})
            continue
        result = run_git(repo_root, ["fetch", "--prune", remote])
        if result.returncode != 0:
            failures.append({"kind": "remote_fetch_failed", "remote": remote, "returncode": result.returncode, "stderr": result.stderr[-2000:]})
    return failures


def pinned_ref_expected_actual(repo_root: Path, label: str, value: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        expected = value.get("head")
        ref = value.get("ref")
        if not ref:
            branch = value.get("branch") or value.get("targetBranch")
            remote = value.get("remote")
            if remote and branch and label in {"remoteFeature", "target"}:
                ref = "refs/remotes/%s/%s" % (remote, branch)
            elif branch:
                ref = "refs/heads/%s" % branch
        if expected and ref:
            actual = rev_parse(repo_root, str(ref), required=False)
            rows.append({"label": label, "ref": str(ref), "expected": str(expected), "actual": actual})
        for child_label, child_value in value.items():
            if isinstance(child_value, (dict, list)):
                rows.extend(pinned_ref_expected_actual(repo_root, "%s.%s" % (label, child_label), child_value))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(pinned_ref_expected_actual(repo_root, "%s[%d]" % (label, index), item))
    return rows


def current_manifest_hash(repo_root: Path, config: Dict[str, Any], work_block_id: Optional[str]) -> Optional[str]:
    if not work_block_id:
        return None
    try:
        return stable_hash(load_manifest(repo_root, config, str(work_block_id)))
    except Exception:
        return None


def packet_report_hash(repo_root: Path, packet: Dict[str, Any]) -> Optional[str]:
    raw_path = packet.get("reportPath")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = repo_root / path
    if not path.exists():
        return None
    try:
        report = read_json(path, {})
    except Exception:
        return None
    if not isinstance(report, dict):
        return None
    return stable_hash({key: value for key, value in report.items() if key != "reportPath"})


def recompute_agent_dispatch_hash(packet: Dict[str, Any]) -> str:
    return stable_hash({key: value for key, value in packet.items() if key not in {"dispatchHash", "queuePath"}})


def agent_remediation_packet_stale_reasons(repo_root: Path, config: Dict[str, Any], packet: Dict[str, Any]) -> List[Dict[str, Any]]:
    queue_config = agent_remediation_queue_config(config)
    reasons: List[Dict[str, Any]] = []
    status = str(packet.get("status", "queued"))
    if status not in {"queued", "pending"}:
        reasons.append({"kind": "queue_packet_not_pending_without_retirement", "status": status, "retirementProof": packet.get("retirementProof"), "validRetirementProof": agent_queue_retirement_proof_valid(repo_root, config, packet)})
    required = ["candidateId", "actionId", "evidenceHash", "policyHash", "pinnedRefs", "shards"]
    for field in required:
        if field not in packet:
            reasons.append({"kind": "missing_field", "field": field})
    if packet.get("policyHash") != config.get("policyHash"):
        reasons.append({"kind": "policy_hash_mismatch", "expected": packet.get("policyHash"), "actual": config.get("policyHash")})
    if packet.get("dirtyStateHash") and packet.get("dirtyStateHash") != agent_remediation_dirty_state_hash(repo_root):
        reasons.append({"kind": "dirty_state_changed", "expected": packet.get("dirtyStateHash"), "actual": agent_remediation_dirty_state_hash(repo_root)})
    if packet.get("dispatchHash") and packet.get("dispatchHash") != recompute_agent_dispatch_hash(packet):
        reasons.append({"kind": "dispatch_hash_mismatch", "expected": packet.get("dispatchHash"), "actual": recompute_agent_dispatch_hash(packet)})
    if packet.get("candidateReportHash"):
        current_report = packet_report_hash(repo_root, packet)
        if current_report != packet.get("candidateReportHash"):
            reasons.append({"kind": "candidate_report_hash_mismatch", "expected": packet.get("candidateReportHash"), "actual": current_report})
    if packet.get("manifestSelectionHash"):
        current_hash = current_manifest_hash(repo_root, config, packet.get("workBlockId"))
        if current_hash != packet.get("manifestSelectionHash"):
            reasons.append({"kind": "manifest_selection_hash_mismatch", "expected": packet.get("manifestSelectionHash"), "actual": current_hash})
    exact_tuple = packet.get("exactTuple")
    if bool(queue_config.get("requireExactTuple", True)):
        expected_tuple = {
            "candidateId": packet.get("candidateId"),
            "actionId": packet.get("actionId"),
            "evidenceHash": packet.get("evidenceHash"),
            "policyHash": packet.get("policyHash"),
            "pinnedRefs": packet.get("pinnedRefs"),
        }
        if not isinstance(exact_tuple, dict):
            reasons.append({"kind": "missing_exact_tuple"})
        elif exact_tuple != expected_tuple:
            reasons.append({"kind": "exact_tuple_mismatch", "expected": expected_tuple, "actual": exact_tuple})
    for row in pinned_ref_expected_actual(repo_root, "pinnedRefs", packet.get("pinnedRefs")):
        if row["expected"] != row["actual"]:
            reasons.append({"kind": "pinned_ref_drifted", **row})
    result_root = agent_remediation_result_root(repo_root, config)
    for shard in packet.get("shards", []) if isinstance(packet.get("shards"), list) else []:
        result_path = shard.get("resultPath") if isinstance(shard, dict) else None
        if not result_path:
            reasons.append({"kind": "missing_shard_result_path", "shardId": shard.get("shardId") if isinstance(shard, dict) else None})
            continue
        path = config_relative_path(repo_root, str(result_path))
        if not path_is_under(path, result_root):
            reasons.append({"kind": "result_path_outside_result_root", "shardId": shard.get("shardId"), "resultPath": str(path), "resultRoot": str(result_root)})
    return reasons


def resolve_agent_result_path(repo_root: Path, config: Dict[str, Any], result_path: str) -> Path:
    path = config_relative_path(repo_root, str(result_path))
    result_root = agent_remediation_result_root(repo_root, config)
    if not path_is_under(path, result_root):
        raise HygieneError("agent result path must stay under resultRoot: %s" % path)
    return path


def write_agent_surface_unavailable_result(
    repo_root: Path,
    config: Dict[str, Any],
    packet: Dict[str, Any],
    shard: Dict[str, Any],
    *,
    surface: str,
) -> Dict[str, Any]:
    queue_config = agent_remediation_queue_config(config)
    status = str(queue_config.get("surfaceUnavailableStatus") or "agent_remediation_surface_unavailable")
    result_path = resolve_agent_result_path(repo_root, config, str(shard.get("resultPath") or ""))
    payload = {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "status": status,
        "createdAt": utc_now(),
        "surface": surface,
        "candidateId": packet.get("candidateId"),
        "shardId": shard.get("shardId"),
        "workBlockId": shard.get("workBlockId") or packet.get("workBlockId"),
        "actionId": packet.get("actionId"),
        "evidenceHash": packet.get("evidenceHash"),
        "policyHash": packet.get("policyHash"),
        "pinnedRefs": packet.get("pinnedRefs"),
        "exactTuple": packet.get("exactTuple"),
        "queuePath": packet.get("queuePath"),
        "allowedReadScope": shard.get("allowedReadScope", []),
        "allowedWriteScope": shard.get("allowedWriteScope", []),
        "summary": "The current surface reported that it cannot spawn background agents for this remediation shard.",
        "changedPaths": [],
        "blockers": [{"kind": status, "recoveryCommand": packet.get("recoveryCommand")}],
        "validation": [],
        "recoveryCommand": packet.get("recoveryCommand"),
    }
    write_json(result_path, payload)
    write_audit(repo_root, config, status, payload, work_block_id=payload.get("workBlockId"), outcome="blocked")
    return {"status": status, "resultPath": str(result_path), "candidateId": payload["candidateId"], "shardId": payload["shardId"]}


def write_agent_result_collection_artifact(repo_root: Path, config: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: value for key, value in result.items() if key not in {"resultCollectionHash", "resultCollectionPath"}}
    collection_hash = stable_hash(payload)
    path = agent_remediation_result_root(repo_root, config) / "collections" / ("%s.json" % collection_hash)
    artifact = {**payload, "resultCollectionHash": collection_hash, "resultCollectionPath": repo_relative_or_absolute(repo_root, path)}
    write_json(path, artifact)
    return {"resultCollectionHash": collection_hash, "resultCollectionPath": repo_relative_or_absolute(repo_root, path)}


def agent_remediation_queue_consumer_plan(
    repo_root_arg: Path,
    *,
    surface: str = "codex-desktop",
    mark_unavailable: bool = False,
) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    queue_config = agent_remediation_queue_config(config)
    if not queue_config:
        return {"status": "disabled", "reason": "agent_remediation_queue_disabled", "spawnPlan": [], "packets": []}
    packets = pending_agent_remediation_queue_packets(repo_root, config)
    remote_failures = refresh_agent_queue_remote_refs(repo_root, config, packets)
    max_parallel = max(1, int(queue_config.get("maxParallelAgents", 1) or 1))
    spawn_plan: List[Dict[str, Any]] = []
    stale_packets: List[Dict[str, Any]] = []
    unavailable_results: List[Dict[str, Any]] = []
    packet_summaries: List[Dict[str, Any]] = []
    for packet in packets:
        stale_reasons = [*remote_failures, *agent_remediation_packet_stale_reasons(repo_root, config, packet)]
        summary = {
            "queuePath": packet.get("queuePath"),
            "candidateId": packet.get("candidateId"),
            "workBlockId": packet.get("workBlockId"),
            "actionId": packet.get("actionId"),
            "staleReasons": stale_reasons,
        }
        packet_summaries.append(summary)
        if stale_reasons:
            stale_packets.append(summary)
            write_audit(repo_root, config, "agent_remediation_queue_stale", summary, work_block_id=packet.get("workBlockId"), outcome="blocked")
            continue
        for shard in packet.get("shards", []):
            result_path = shard.get("resultPath")
            result_exists = False
            if result_path:
                try:
                    result_exists = resolve_agent_result_path(repo_root, config, str(result_path)).exists()
                except HygieneError:
                    result_exists = False
            if result_exists:
                continue
            if mark_unavailable:
                unavailable_results.append(write_agent_surface_unavailable_result(repo_root, config, packet, shard, surface=surface))
                continue
            if len(spawn_plan) >= max_parallel:
                continue
            spawn_plan.append(
                {
                    "surface": surface,
                    "candidateId": packet.get("candidateId"),
                    "shardId": shard.get("shardId"),
                    "workBlockId": shard.get("workBlockId") or packet.get("workBlockId"),
                    "actionId": packet.get("actionId"),
                    "exactTuple": packet.get("exactTuple"),
                    "queuePath": packet.get("queuePath"),
                    "allowedReadScope": shard.get("allowedReadScope", []),
                    "allowedWriteScope": shard.get("allowedWriteScope", []),
                    "expectedOutputSchema": shard.get("expectedOutputSchema") or packet.get("expectedOutputSchema"),
                    "validationRequirements": shard.get("validationRequirements") or packet.get("validationRequirements", []),
                    "resultPath": shard.get("resultPath"),
                    "recoveryCommand": shard.get("recoveryCommand") or packet.get("recoveryCommand"),
                    "perAgentTimeoutMs": queue_config["perAgentTimeoutMs"],
                    "maxAgentOutputBytes": queue_config["maxAgentOutputBytes"],
                    "mutationBoundary": "repo-owned symbolic actors only",
                }
            )
    status = "blocked" if stale_packets else "success"
    if mark_unavailable and unavailable_results:
        status = "blocked" if stale_packets else str(queue_config.get("surfaceUnavailableStatus") or "agent_remediation_surface_unavailable")
    result = {
        "status": status,
        "surface": surface,
        "queueRoots": [str(path) for path in agent_remediation_queue_roots(repo_root, config)],
        "resultRoot": str(agent_remediation_result_root(repo_root, config)),
        "maxParallelAgents": max_parallel,
        "packetCount": len(packets),
        "packets": packet_summaries,
        "spawnPlan": spawn_plan,
        "stalePackets": stale_packets,
        "unavailableResults": unavailable_results,
    }
    write_audit(repo_root, config, "agent_remediation_queue_consumer", result, outcome="success" if status == "success" else "blocked")
    return result


def collect_agent_remediation_results(repo_root_arg: Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    packets = pending_agent_remediation_queue_packets(repo_root, config)
    remote_failures = refresh_agent_queue_remote_refs(repo_root, config, packets)
    collected: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    for packet in packets:
        stale_reasons = [*remote_failures, *agent_remediation_packet_stale_reasons(repo_root, config, packet)]
        if stale_reasons:
            blockers.append({"kind": "stale_queue_packet", "candidateId": packet.get("candidateId"), "queuePath": packet.get("queuePath"), "staleReasons": stale_reasons})
            continue
        for shard in packet.get("shards", []):
            try:
                result_path = resolve_agent_result_path(repo_root, config, str(shard.get("resultPath") or ""))
            except HygieneError as exc:
                blockers.append({"kind": "agent_result_path_invalid", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "error": str(exc)})
                continue
            if not result_path.exists():
                blockers.append({"kind": "missing_agent_result", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "resultPath": str(result_path)})
                continue
            try:
                payload = read_json(result_path, {})
            except Exception as exc:
                blockers.append({"kind": "unreadable_agent_result", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "resultPath": str(result_path), "error": str(exc)})
                continue
            schema = shard.get("expectedOutputSchema") if isinstance(shard.get("expectedOutputSchema"), dict) else packet.get("expectedOutputSchema")
            required_fields = list((schema or agent_result_packet_schema()).get("requiredFields", agent_result_packet_schema()["requiredFields"]))
            missing_fields = [field for field in required_fields if field not in payload]
            invalid_list_fields = [
                field
                for field in ["changedPaths", "blockers", "validation"]
                if field in payload and not isinstance(payload.get(field), list)
            ]
            if missing_fields or invalid_list_fields:
                blockers.append(
                    {
                        "kind": "agent_result_schema_invalid",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "missingFields": missing_fields,
                        "invalidListFields": invalid_list_fields,
                        "resultPath": str(result_path),
                    }
                )
            expected_identity = {
                "candidateId": packet.get("candidateId"),
                "shardId": shard.get("shardId"),
                "workBlockId": shard.get("workBlockId") or packet.get("workBlockId"),
                "actionId": packet.get("actionId"),
            }
            actual_identity = {
                "candidateId": payload.get("candidateId"),
                "shardId": payload.get("shardId"),
                "workBlockId": payload.get("workBlockId"),
                "actionId": payload.get("actionId"),
            }
            if actual_identity != expected_identity:
                blockers.append(
                    {
                        "kind": "agent_result_identity_mismatch",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "expected": expected_identity,
                        "actual": actual_identity,
                        "resultPath": str(result_path),
                    }
                )
            expected_tuple = packet.get("exactTuple")
            actual_tuple = payload.get("exactTuple") or {
                "candidateId": payload.get("candidateId"),
                "actionId": payload.get("actionId"),
                "evidenceHash": payload.get("evidenceHash"),
                "policyHash": payload.get("policyHash"),
                "pinnedRefs": payload.get("pinnedRefs"),
            }
            if actual_tuple != expected_tuple:
                blockers.append({"kind": "agent_result_tuple_mismatch", "candidateId": packet.get("candidateId"), "shardId": shard.get("shardId"), "expected": expected_tuple, "actual": actual_tuple})
            payload_status = str(payload.get("status") or "")
            payload_blockers = payload.get("blockers", [])
            if payload_status != "resolved":
                blockers.append(
                    {
                        "kind": "agent_result_not_resolved",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "status": payload_status,
                        "resultPath": str(result_path),
                    }
                )
            if payload_status in {"blocked", "agent_remediation_surface_unavailable"} or payload_blockers:
                blockers.append(
                    {
                        "kind": "agent_result_blocked",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "status": payload_status,
                        "blockers": payload_blockers,
                        "resultPath": str(result_path),
                    }
                )
            failed_validation = [item for item in payload.get("validation", []) if isinstance(item, dict) and int(item.get("returncode", 0) or 0) != 0]
            if failed_validation:
                blockers.append(
                    {
                        "kind": "agent_result_validation_failed",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "validation": failed_validation,
                        "resultPath": str(result_path),
                    }
                )
            allowed_write = {normalize_rel(str(path)) for path in shard.get("allowedWriteScope", []) if normalize_rel(str(path))}
            changed = {normalize_rel(str(path)) for path in payload.get("changedPaths", []) if normalize_rel(str(path))}
            outside = sorted(changed - allowed_write)
            if outside:
                blockers.append(
                    {
                        "kind": "agent_scope_violation",
                        "candidateId": packet.get("candidateId"),
                        "shardId": shard.get("shardId"),
                        "outsideWriteScope": outside,
                        "allowedWriteScope": sorted(allowed_write),
                        "resultPath": str(result_path),
                    }
                )
            collected.append(
                {
                    "candidateId": packet.get("candidateId"),
                    "shardId": shard.get("shardId"),
                    "workBlockId": shard.get("workBlockId") or packet.get("workBlockId"),
                    "actionId": packet.get("actionId"),
                    "evidenceHash": packet.get("evidenceHash"),
                    "policyHash": packet.get("policyHash"),
                    "pinnedRefs": packet.get("pinnedRefs"),
                    "exactTuple": packet.get("exactTuple"),
                    "resultPath": str(result_path),
                    "status": payload.get("status"),
                    "changedPaths": sorted(changed),
                    "blockers": payload.get("blockers", []),
                }
            )
    status = "blocked" if blockers else ("success" if collected else "pending")
    result = {
        "status": status,
        "packetCount": len(packets),
        "collectedResults": collected,
        "blockers": blockers,
        "nextSymbolicAction": None if blockers else {
            "action": "repo_owned_coordinator_revalidate_and_finalize",
            "allowedActors": ["repo-sweep", "finalize-closeout", "remediate-retained"],
            "mutationBoundary": "repo-owned symbolic actors only",
        },
    }
    result.update(write_agent_result_collection_artifact(repo_root, config, result))
    write_audit(repo_root, config, "agent_remediation_result_collection", result, outcome="success" if status == "success" else "blocked")
    return result


def retire_stale_plan_absent_agent_queue_packets(repo_root_arg: Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    packets = pending_agent_remediation_queue_packets(repo_root, config)
    remote_failures = refresh_agent_queue_remote_refs(repo_root, config, packets)
    if remote_failures:
        result = {
            "status": "blocked",
            "reason": "remote_fetch_failed",
            "packetCount": len(packets),
            "currentCandidateIds": [],
            "retiredPackets": [],
            "skippedPackets": [],
            "blockers": [{"kind": "remote_fetch_failed", "remoteFailures": remote_failures}],
        }
        write_audit(repo_root, config, "agent_remediation_queue_plan_absent_retirement", result, outcome="blocked")
        return result
    plan_result = repo_sweep(repo_root, apply=False)
    if plan_result.get("status") != "planned":
        result = {
            "status": "blocked",
            "reason": "repo_sweep_plan_unavailable",
            "planStatus": plan_result.get("status"),
            "planReason": plan_result.get("reason"),
            "packets": [],
            "retiredPackets": [],
            "blockers": [{"kind": "repo_sweep_plan_unavailable", "plan": plan_result}],
        }
        write_audit(repo_root, config, "agent_remediation_queue_plan_absent_retirement", result, outcome="blocked")
        return result
    apply_scope = plan_result.get("applyScope") if isinstance(plan_result.get("applyScope"), dict) else {}
    current_candidate_ids = {str(item) for item in (apply_scope.get("candidateIds") or []) if str(item)}
    current_candidate_ids.update(str(item.get("candidateId")) for item in plan_result.get("promotedCandidates", []) if isinstance(item, dict) and item.get("candidateId"))
    retired: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for packet in packets:
        candidate_id = str(packet.get("candidateId") or "")
        stale_reasons = [*remote_failures, *agent_remediation_packet_stale_reasons(repo_root, config, packet)]
        summary = {
            "candidateId": candidate_id,
            "queuePath": packet.get("queuePath"),
            "actionId": packet.get("actionId"),
            "staleReasons": stale_reasons,
        }
        if not stale_reasons:
            skipped.append({**summary, "reason": "packet_not_stale"})
            continue
        if candidate_id in current_candidate_ids:
            blockers.append({**summary, "kind": "stale_packet_still_present_in_current_plan"})
            continue
        exact_tuple = packet.get("exactTuple")
        if not isinstance(exact_tuple, dict):
            blockers.append({**summary, "kind": "missing_exact_tuple"})
            continue
        queue_path = Path(str(packet.get("queuePath") or ""))
        if not queue_path.exists():
            blockers.append({**summary, "kind": "queue_path_missing"})
            continue
        collection_payload = {
            "status": "success",
            "packetCount": 1,
            "retirementReason": "stale_plan_absent_queue_packet",
            "currentPolicyHash": config.get("policyHash"),
            "currentPlanEvidenceHash": plan_result.get("plan", {}).get("evidenceHash"),
            "currentApplyScope": apply_scope,
            "collectedResults": [
                {
                    "candidateId": packet.get("candidateId"),
                    "shardId": "plan-absent-retirement",
                    "workBlockId": packet.get("workBlockId"),
                    "actionId": packet.get("actionId"),
                    "evidenceHash": packet.get("evidenceHash"),
                    "policyHash": config.get("policyHash"),
                    "pinnedRefs": packet.get("pinnedRefs"),
                    "exactTuple": exact_tuple,
                    "resultPath": packet.get("queuePath"),
                    "status": "retired_plan_absent",
                    "changedPaths": [],
                    "blockers": [],
                    "staleReasons": stale_reasons,
                }
            ],
            "blockers": [],
            "nextSymbolicAction": {
                "action": "repo_owned_coordinator_revalidate_and_finalize",
                "allowedActors": ["repo-sweep", "finalize-closeout", "remediate-retained"],
                "mutationBoundary": "repo-owned symbolic actors only",
            },
        }
        collection = write_agent_result_collection_artifact(repo_root, config, collection_payload)
        retired_packet = dict(packet)
        retired_packet["status"] = "retired"
        retired_packet["retiredAt"] = utc_now()
        retired_packet["retirementReason"] = "stale_plan_absent_queue_packet"
        retired_packet["retirementProof"] = {
            "candidateId": packet.get("candidateId"),
            "actionId": packet.get("actionId"),
            "evidenceHash": packet.get("evidenceHash"),
            "policyHash": config.get("policyHash"),
            "pinnedRefs": packet.get("pinnedRefs"),
            "exactTuple": exact_tuple,
            "resultCollectionStatus": "success",
            "resultCollectionHash": collection["resultCollectionHash"],
            "resultCollectionPath": collection["resultCollectionPath"],
            "retiredPacketStatus": "retired",
            "retirementReason": "stale_plan_absent_queue_packet",
        }
        write_json(queue_path, retired_packet)
        row = {
            **summary,
            "status": "retired",
            "retirementProof": retired_packet["retirementProof"],
            "resultCollectionHash": collection["resultCollectionHash"],
            "resultCollectionPath": collection["resultCollectionPath"],
        }
        retired.append(row)
        write_audit(repo_root, config, "agent_remediation_queue_packet_retired", row, work_block_id=packet.get("workBlockId"), outcome="success")
    status = "blocked" if blockers else ("success" if retired else "no_op")
    result = {
        "status": status,
        "reason": "stale_plan_absent_queue_packet_retirement" if retired else "no_stale_plan_absent_packets_retired",
        "packetCount": len(packets),
        "currentCandidateIds": sorted(current_candidate_ids),
        "retiredPackets": retired,
        "skippedPackets": skipped,
        "blockers": blockers,
    }
    write_audit(repo_root, config, "agent_remediation_queue_plan_absent_retirement", result, outcome="success" if status == "success" else ("recorded" if status == "no_op" else "blocked"))
    return result


def cleanup_branch_after_sweep_action(
    repo_root: Path,
    config: Dict[str, Any],
    plan: Dict[str, Any],
    item: Dict[str, Any],
    *,
    force_branch_delete: bool = False,
    allow_protected_worktree: bool = False,
    recovery_artifact: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    branch = str(item["branch"])
    worktree = item.get("worktree") or {}
    target_branch = str(plan["pinnedRefs"]["target"]["branch"])
    branch_head_for_recovery = str(item.get("head") or "")
    target_head_for_recovery = str(plan["pinnedRefs"]["target"]["head"])
    recovery_verification = verify_prune_recovery_artifact(
        repo_root,
        config,
        recovery_artifact,
        candidate_id=str((recovery_artifact or {}).get("candidateId") or ""),
        branch_head=branch_head_for_recovery,
        target_head=target_head_for_recovery,
    )
    if not recovery_verification["ok"]:
        action = {
            "action": "retain_prune_recovery_artifact_invalid",
            "branch": branch,
            "returncode": 1,
            "recoveryArtifact": recovery_artifact,
            "recoveryVerification": recovery_verification,
        }
        actions.append(action)
        write_audit(repo_root, config, "manual_prune_recovery", action, outcome="blocked")
        return actions
    if item.get("checkedOut"):
        path = Path(str(worktree.get("path") or repo_root))
        dirty = worktree_dirty_state(path)
        if dirty.get("dirty"):
            action = {"action": "retain_checked_out_dirty_branch", "branch": branch, "dirty": dirty, "returncode": 1}
            actions.append(action)
            write_audit(repo_root, config, "cleanup_retention", action, outcome="retained")
            return actions
        if path.resolve() == repo_root.resolve():
            switch = run_git(repo_root, ["switch", target_branch])
            action = {"action": "switch_sweep_worktree_to_target", "branch": branch, "targetBranch": target_branch, "returncode": switch.returncode, "stderr": switch.stderr[-2000:]}
            actions.append(action)
            if switch.returncode != 0:
                write_audit(repo_root, config, "cleanup_retention", action, outcome="retained")
                return actions
        else:
            if is_protected_worktree_path(repo_root, config, path) and not allow_protected_worktree:
                action = {"action": "retain_protected_worktree", "branch": branch, "path": str(path), "returncode": 1, "reason": "protected_worktree_root"}
                actions.append(action)
                write_audit(repo_root, config, "cleanup_retention", action, outcome="retained")
                return actions
            action = {"action": "remove_branch_worktree", **remove_worktree(repo_root, path)}
            actions.append(action)
            write_audit(repo_root, config, "worktree_deletion", action, outcome="success" if action["returncode"] == 0 else "blocked")
            if action["returncode"] != 0:
                return actions
    force_reason = None
    if not force_branch_delete:
        branch_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
        target_head = rev_parse(repo_root, f"refs/heads/{target_branch}", required=False) or str(plan["pinnedRefs"]["target"]["head"])
        if branch_head and target_head and is_ancestor(repo_root, branch_head, target_head):
            force_branch_delete = current_branch(repo_root) != target_branch
            force_reason = "branch_head_is_ancestor_of_target"
    delete_flag = "-D" if force_branch_delete else "-d"
    delete = run_git(repo_root, ["branch", delete_flag, branch])
    action = {"action": "delete_local_branch", "branch": branch, "forced": force_branch_delete, "forceReason": force_reason, "returncode": delete.returncode, "stderr": delete.stderr[-2000:], "recoveryArtifact": recovery_artifact}
    actions.append(action)
    write_audit(repo_root, config, "branch_deletion", action, outcome="success" if delete.returncode == 0 else "blocked")
    return actions


def cleanup_foreign_dirty_integrated_branch(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    branch = str(item["branch"])
    branch_head = str(item["head"])
    target = plan["pinnedRefs"]["target"]
    target_head = str(target["head"])
    target_branch = str(target["branch"])
    worktree = item.get("worktree") or {}
    worktree_path = Path(str(worktree.get("path") or repo_root))
    if not is_ancestor(repo_root, branch_head, target_head):
        action = {"status": "blocked", "reason": "branch_not_ancestor_of_target", "branch": branch, "head": branch_head, "targetHead": target_head}
        write_audit(repo_root, config, "blocked_repair", action, outcome="blocked")
        return action
    current_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    if current_head != branch_head:
        action = {"status": "blocked", "reason": "branch_head_drifted", "branch": branch, "expected": branch_head, "actual": current_head}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    classification = classify_dirty_entries_for_branch(
        repo_root,
        config,
        branch=branch,
        branch_head=branch_head,
        target_head=target_head,
        worktree_path=worktree_path,
    )
    if classification["ownedDirty"] or classification.get("mixedDirty") or classification["unownedDirty"]:
        action = {"status": "blocked", "reason": "dirty_classification_changed", "classification": classification, "report": report}
        write_audit(repo_root, config, "blocked_repair", action, outcome="blocked")
        return action
    target_delta = ref_delta_paths(repo_root, branch_head, target_head)
    overlap = sorted(set(classification["dirtyPaths"]) & set(target_delta))
    if overlap:
        action = {"status": "blocked", "reason": "foreign_dirty_target_overlap", "overlap": overlap, "classification": classification}
        write_audit(repo_root, config, "cleanup_retention", action, outcome="retained")
        return action
    if worktree_path.resolve() == repo_root.resolve():
        switch = run_git(worktree_path, ["switch", target_branch])
        switch_action = {"action": "switch_foreign_dirty_worktree_to_target", "branch": branch, "targetBranch": target_branch, "returncode": switch.returncode, "stderr": switch.stderr[-2000:]}
    else:
        switch = run_git(worktree_path, ["switch", "--detach", target_head])
        switch_action = {"action": "detach_foreign_dirty_worktree_at_target", "branch": branch, "targetHead": target_head, "path": str(worktree_path), "returncode": switch.returncode, "stderr": switch.stderr[-2000:]}
    if switch.returncode != 0:
        payload = {"status": "blocked", "reason": "target_switch_failed", "switch": switch_action, "classification": classification}
        write_audit(repo_root, config, "cleanup_retention", payload, outcome="retained")
        return payload
    delete = run_git(repo_root, ["branch", "-D", branch])
    delete_action = {"action": "delete_local_branch", "branch": branch, "forced": True, "forceReason": "foreign_dirty_worktree_switched_to_target", "returncode": delete.returncode, "stderr": delete.stderr[-2000:]}
    write_audit(repo_root, config, "branch_deletion", delete_action, outcome="success" if delete.returncode == 0 else "blocked")
    result = {
        "status": "success" if delete.returncode == 0 else "blocked",
        "action": "foreign_dirty_integrated_branch_prune",
        "branch": branch,
        "switch": switch_action,
        "delete": delete_action,
        "retainedForeignDirty": classification["foreignDirty"],
    }
    write_audit(repo_root, config, "cleanup_retention", {"action": "retained_foreign_dirty_after_branch_prune", **result}, outcome="retained")
    return result


def detached_false_dirty_proof(worktree_path: Path, entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    proofs: List[Dict[str, Any]] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        status = str(entry.get("status") or "")
        if status != " M" or not path:
            return {"eligible": False, "proofs": proofs, "reason": "non_worktree_only_modification"}
        working_diff = run_git(worktree_path, ["diff", "--quiet", "--", path])
        cached_diff = run_git(worktree_path, ["diff", "--cached", "--quiet", "--", path])
        if working_diff.returncode != 0 or cached_diff.returncode != 0:
            return {"eligible": False, "proofs": proofs, "reason": "material_diff_present", "path": path}
        index_object = run_git(worktree_path, ["rev-parse", f":{path}"])
        proofs.append(
            {
                "path": path,
                "status": status,
                "workingDiffQuiet": True,
                "cachedDiffQuiet": True,
                "indexObjectId": index_object.stdout.strip() if index_object.returncode == 0 else None,
                "worktreeObjectId": git_hash_object_id(worktree_path, path),
            }
        )
    return {"eligible": bool(proofs), "proofs": proofs, "reason": "diff_quiet_false_dirty" if proofs else "no_entries"}


def apply_detached_dirty_preserve(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    worktree = report.get("worktree") or {}
    worktree_path = Path(str(worktree.get("path") or ""))
    if not worktree_path.exists():
        action = {"status": "blocked", "reason": "detached_worktree_missing", "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    current_entries = parse_status_paths(worktree_path)
    current_paths = sorted({str(item["path"]) for item in current_entries})
    expected_paths = sorted(report.get("scope", {}).get("dirtyPaths") or [])
    if current_paths != expected_paths:
        action = {"status": "blocked", "reason": "detached_dirty_tuple_drifted", "expectedPaths": expected_paths, "actualPaths": current_paths, "report": report}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    sensitive_paths = [path for path in current_paths if path_matches_any(path, config.get("paths", {}).get("sensitive", []))]
    if sensitive_paths and not bool(blocker_auto_remediation_config(config).get("allowSensitiveDetachedDirtyPreservation", False)):
        action = {"status": "blocked", "reason": "sensitive_dirty_paths", "sensitivePaths": sensitive_paths, "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    branch = str(report.get("preservationBranch") or detached_dirty_preservation_branch(config, str(report["candidateId"])))
    base_head = str(report.get("head") or worktree.get("head") or "")
    existing_branch_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    preservation_path = closeout_state_root(repo_root, config) / "repo-sweep" / "detached-preserve" / safe_state_name(branch)
    false_dirty = detached_false_dirty_proof(worktree_path, current_entries)
    if false_dirty.get("eligible"):
        provisional_cleanup: Dict[str, Any] = {"status": "not_present"}
        if existing_branch_head and existing_branch_head != base_head:
            action = {"status": "blocked", "reason": "preservation_branch_exists", "branch": branch, "head": existing_branch_head, "expected": base_head, "falseDirtyProof": false_dirty}
            write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
            return action
        if preservation_path.exists():
            preservation_dirty = worktree_dirty_state(preservation_path)
            if preservation_dirty.get("dirty"):
                action = {"status": "blocked", "reason": "false_dirty_provisional_worktree_dirty", "path": str(preservation_path), "dirtyState": preservation_dirty, "falseDirtyProof": false_dirty}
                write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
                return action
            provisional_cleanup = remove_worktree(repo_root, preservation_path)
            if provisional_cleanup.get("returncode") != 0:
                action = {"status": "blocked", "reason": "false_dirty_provisional_cleanup_failed", "cleanup": provisional_cleanup, "falseDirtyProof": false_dirty}
                write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
                return action
        branch_cleanup: Dict[str, Any] = {"status": "not_present"}
        if existing_branch_head:
            delete_branch = run_git(repo_root, ["branch", "-D", branch])
            branch_cleanup = {"branch": branch, "head": existing_branch_head, "returncode": delete_branch.returncode, "stderr": delete_branch.stderr[-2000:]}
            if delete_branch.returncode != 0:
                action = {"status": "blocked", "reason": "false_dirty_provisional_branch_cleanup_failed", "branchCleanup": branch_cleanup, "falseDirtyProof": false_dirty}
                write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
                return action
        original_cleanup = remove_worktree(repo_root, worktree_path)
        result = {
            "status": "success" if original_cleanup.get("returncode") == 0 else "blocked",
            "action": "detached_false_dirty_prune",
            "reason": "tracked_worktree_entries_have_no_cached_or_working_tree_diff",
            "falseDirtyProof": false_dirty,
            "provisionalWorktreeCleanup": provisional_cleanup,
            "provisionalBranchCleanup": branch_cleanup,
            "originalWorktreeCleanup": original_cleanup,
        }
        write_audit(repo_root, config, "orphan_quarantine", result, outcome="success" if result["status"] == "success" else "blocked")
        return result
    if existing_branch_head and existing_branch_head != base_head:
        action = {"status": "blocked", "reason": "preservation_branch_exists", "branch": branch, "head": existing_branch_head, "expected": base_head}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    if preservation_path.exists():
        action = {"status": "blocked", "reason": "preservation_worktree_exists", "path": str(preservation_path), "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    add_args = ["worktree", "add", "--no-checkout"]
    if existing_branch_head:
        add_args.extend([str(preservation_path), branch])
    else:
        add_args.extend(["-b", branch, str(preservation_path), base_head])
    add = run_git_longpaths(repo_root, add_args)
    if add.returncode != 0:
        action = {"status": "blocked", "reason": "preservation_worktree_failed", "returncode": add.returncode, "stderr": add.stderr[-3000:], "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    copied: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    try:
        for operation, args in [
            ("sparse_checkout_init", ["sparse-checkout", "init", "--no-cone"]),
            ("sparse_checkout_set", ["sparse-checkout", "set", "--no-cone", *current_paths]),
            ("sparse_checkout_checkout", ["checkout"]),
        ]:
            result = run_git(preservation_path, args)
            if result.returncode != 0:
                cleanup = remove_worktree(repo_root, preservation_path)
                action = {"status": "blocked", "reason": "preservation_sparse_checkout_failed", "operation": operation, "returncode": result.returncode, "stderr": result.stderr[-3000:], "cleanup": cleanup}
                write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
                return action
        entries_by_path = {str(item["path"]): item for item in current_entries}
        for path in current_paths:
            copied.append(copy_exact_path_for_split(worktree_path, preservation_path, path, str(entries_by_path[path]["status"])))
        add_preserved = run_git(preservation_path, ["add", "--", *current_paths])
        if add_preserved.returncode != 0:
            action = {"status": "blocked", "reason": "preservation_add_failed", "returncode": add_preserved.returncode, "stderr": add_preserved.stderr[-3000:], "copied": copied}
            write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
            return action
        commit = run_git(preservation_path, ["commit", "-m", "preserve detached dirty worktree"])
        if commit.returncode != 0:
            action = {"status": "blocked", "reason": "preservation_commit_failed", "returncode": commit.returncode, "stdout": commit.stdout[-3000:], "stderr": commit.stderr[-3000:], "copied": copied}
            write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
            return action
        commit_head = git_stdout(preservation_path, ["rev-parse", "HEAD"])
        verification = verify_detached_preservation_commit(repo_root, branch=branch, commit_head=commit_head, entries_by_path=entries_by_path)
        if not verification["ok"]:
            action = {
                "status": "blocked",
                "reason": "preservation_commit_stale_or_incomplete",
                "verification": verification,
                "copied": copied,
                "report": report,
            }
            write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
            return action
        dirty_recovery = write_dirty_worktree_recovery_evidence(
            repo_root,
            config,
            worktree_path=worktree_path,
            report=report,
            entries=current_entries,
            preservation_branch=branch,
            preservation_head=commit_head,
        )
        if dirty_recovery.get("status") == "blocked":
            action = {
                "status": "blocked",
                "reason": str(dirty_recovery.get("reason") or "dirty_worktree_recovery_failed"),
                "dirtyRecovery": dirty_recovery,
                "preservationBranch": branch,
                "preservationHead": commit_head,
                "report": report,
            }
            write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
            return action
        for path in current_paths:
            removal = remove_exact_path_from_worktree(worktree_path, path)
            removed.append(removal)
            if removal.get("returncode") != 0:
                action = {"status": "blocked", "reason": "original_cleanup_failed", "preservationBranch": branch, "preservationHead": commit_head, "removed": removed}
                write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
                return action
        remaining_dirty = worktree_dirty_state(worktree_path)
        remove_original = remove_worktree(repo_root, worktree_path) if not remaining_dirty.get("dirty") else {"returncode": 1, "stderr": "worktree still dirty", "path": str(worktree_path)}
        result = {
            "status": "success" if remove_original.get("returncode") == 0 else "blocked",
            "action": "detached_dirty_preserve",
            "preservationBranch": branch,
            "preservationHead": commit_head,
            "preservationWorktree": str(preservation_path),
            "dirtyRecovery": dirty_recovery,
            "copied": copied,
            "removedFromOriginal": removed,
            "originalWorktreeCleanup": remove_original,
        }
        write_audit(repo_root, config, "orphan_quarantine", result, outcome="success" if result["status"] == "success" else "blocked")
        return result
    except Exception as exc:
        action = {"status": "blocked", "reason": "detached_dirty_preserve_exception", "error": str(exc), "copied": copied, "removed": removed, "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action


def apply_generated_only_detached_dirty_prune(repo_root: Path, config: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    worktree = report.get("worktree") or {}
    worktree_path = Path(str(worktree.get("path") or ""))
    if not worktree_path.exists():
        action = {"status": "blocked", "reason": "detached_worktree_missing", "report": report}
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    current_entries = parse_status_paths(worktree_path)
    current_paths = sorted({str(item["path"]) for item in current_entries})
    expected_paths = sorted(report.get("scope", {}).get("dirtyPaths") or [])
    proof = generated_only_detached_dirty_proof(config, current_entries)
    if current_paths != expected_paths:
        action = {
            "status": "blocked",
            "reason": "generated_only_detached_dirty_tuple_drifted",
            "expectedPaths": expected_paths,
            "actualPaths": current_paths,
            "generatedOnlyProof": proof,
            "report": report,
        }
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    if not proof["eligible"]:
        action = {
            "status": "blocked",
            "reason": "generated_only_detached_dirty_unmatched_paths",
            "generatedOnlyProof": proof,
            "report": report,
        }
        write_audit(repo_root, config, "orphan_quarantine", action, outcome="blocked")
        return action
    cleanup = remove_worktree(repo_root, worktree_path)
    action = {
        "status": "success" if cleanup.get("returncode") == 0 else "blocked",
        "action": "generated_only_detached_dirty_prune",
        "worktree": str(worktree_path),
        "generatedOnlyProof": proof,
        "originalWorktreeCleanup": cleanup,
    }
    write_audit(repo_root, config, "orphan_quarantine", action, outcome="success" if action["status"] == "success" else "blocked")
    return action


def apply_repo_sweep_clean_integrate(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    target = target_ref_for(repo_root, config)
    branch = str(item["branch"])
    branch_head = str(item["head"])
    target_branch = str(plan["pinnedRefs"]["target"]["branch"])
    if target["head"] != plan["pinnedRefs"]["target"]["head"]:
        return {"status": "blocked", "reason": "target_head_drifted", "expected": plan["pinnedRefs"]["target"], "actual": target}
    current_head = rev_parse(repo_root, f"refs/heads/{branch}", required=False)
    if current_head != branch_head:
        return {"status": "blocked", "reason": "branch_head_drifted", "branch": branch, "expected": branch_head, "actual": current_head}
    integration_path = closeout_state_root(repo_root, config) / "rs-iw" / uuid.uuid4().hex[:12]
    integration_path.parent.mkdir(parents=True, exist_ok=True)
    add = run_git_longpaths(repo_root, ["worktree", "add", "--detach", str(integration_path), target["head"]])
    if add.returncode != 0:
        return {"status": "blocked", "reason": "integration_worktree_failed", "returncode": add.returncode, "stderr": add.stderr[-2000:]}
    try:
        merge = run_git(
            integration_path,
            ["merge", "--no-ff", "-m", closeout_merge_commit_message(branch, target_branch), branch_head],
        )
        if merge.returncode != 0:
            return {"status": "blocked", "reason": "merge_failed", "returncode": merge.returncode, "stdout": merge.stdout[-2000:], "stderr": merge.stderr[-2000:]}
        diff_check = run_git(integration_path, ["diff", "--check"])
        if diff_check.returncode != 0:
            return {"status": "blocked", "reason": "diff_check_failed", "returncode": diff_check.returncode, "stdout": diff_check.stdout[-2000:], "stderr": diff_check.stderr[-2000:]}
        validations = run_validations(repo_root, config, integration_path)
        failed = next((row for row in validations if row["returncode"] != 0), None)
        if failed:
            return {"status": "blocked", "reason": "validation_failed", "validations": validations}
        new_head = git_stdout(integration_path, ["rev-parse", "HEAD"])
        push_result: Optional[Dict[str, Any]] = None
        recovery: Optional[Dict[str, Any]] = None
        local_update: Optional[Dict[str, Any]] = None
        if target["mode"] == "remote":
            push = run_git(integration_path, ["push", str(target.get("remote")), f"HEAD:{target_branch}"])
            push_result = {"remote": target.get("remote"), "targetBranch": target_branch, "returncode": push.returncode, "stdout": push.stdout[-2000:], "stderr": push.stderr[-2000:]}
            if push.returncode != 0:
                recovery = repair_target_push_failure(
                    repo_root,
                    config,
                    target_branch=target_branch,
                    remote=str(target.get("remote")),
                    attempted_head=new_head,
                    push_result=push_result,
                )
                if recovery["status"] != "success":
                    return {"status": "blocked", "reason": recovery["reason"], "push": push_result, "recovery": recovery}
                new_head = str(recovery["targetHeadAfter"])
                local_update = recovery.get("localTargetUpdate")
        if local_update is None:
            local_update = update_local_target(repo_root, target_branch, new_head)
        if local_update["returncode"] != 0:
            return {"status": "blocked", "reason": "local_target_update_failed", "localUpdate": local_update, "push": push_result}
        cleanup = cleanup_branch_after_sweep_action(repo_root, config, plan, item)
        result = {"status": "success", "action": "clean_integrate", "branch": branch, "newTargetHead": new_head, "validations": validations, "push": push_result, "recovery": recovery, "localUpdate": local_update, "cleanup": cleanup}
        write_audit(repo_root, config, "success", result, outcome="success")
        return result
    finally:
        removal = remove_worktree(repo_root, integration_path)
        write_audit(repo_root, config, "snapshot_pruning", {"action": "integration_worktree_remove", **removal}, outcome="success" if removal["returncode"] == 0 else "blocked")


def remote_provider_ref_snapshot(repo_root: Path, *, remote: str, provider_ref: str) -> Dict[str, Any]:
    query = run_git(repo_root, ["ls-remote", "--exit-code", "--heads", remote, provider_ref])
    stdout = query.stdout.strip()
    if query.returncode == 2 and not stdout:
        return {"status": "absent", "returncode": query.returncode, "head": None, "ref": provider_ref}
    if query.returncode != 0:
        return {
            "status": "blocked",
            "reason": "remote_feature_exact_query_failed",
            "returncode": query.returncode,
            "ref": provider_ref,
            "stdout": query.stdout[-2000:],
            "stderr": query.stderr[-2000:],
        }
    rows = [line.split() for line in stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != provider_ref or not re.fullmatch(r"[0-9a-f]{40}", rows[0][0]):
        return {
            "status": "blocked",
            "reason": "remote_feature_exact_query_malformed",
            "returncode": query.returncode,
            "ref": provider_ref,
            "stdout": query.stdout[-2000:],
            "stderr": query.stderr[-2000:],
        }
    return {"status": "present", "returncode": query.returncode, "head": rows[0][0], "ref": provider_ref}


def delete_remote_feature_ref(repo_root: Path, config: Dict[str, Any], *, remote: str, branch: str, expected_head: str) -> Dict[str, Any]:
    provider_ref = f"refs/heads/{branch}"
    before = remote_provider_ref_snapshot(repo_root, remote=remote, provider_ref=provider_ref)
    if before["status"] == "blocked":
        action = {
            "status": "blocked",
            "reason": before["reason"],
            "action": "delete_remote_branch",
            "remote": remote,
            "branch": branch,
            "expectedHead": expected_head,
            "providerQueryBefore": before,
        }
        write_audit(repo_root, config, "remote_branch_deletion", action, outcome="blocked")
        return action
    if before["status"] == "absent":
        tracking_cleanup = run_git(repo_root, ["update-ref", "-d", f"refs/remotes/{remote}/{branch}"])
        action = {
            "status": "success" if tracking_cleanup.returncode == 0 else "blocked",
            "action": "delete_remote_branch",
            "remote": remote,
            "branch": branch,
            "alreadyMissing": True,
            "expectedHead": expected_head,
            "providerQueryBefore": before,
            "trackingCleanupReturncode": tracking_cleanup.returncode,
        }
        if action["status"] != "success":
            action["reason"] = "remote_tracking_ref_cleanup_failed"
        write_audit(repo_root, config, "remote_branch_deletion", action, outcome=action["status"])
        return action
    if before["head"] != expected_head:
        action = {"status": "blocked", "reason": "remote_feature_head_drifted", "remote": remote, "branch": branch, "expected": expected_head, "actual": before["head"], "providerQueryBefore": before}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    delete = run_git(
        repo_root,
        [
            "push",
            f"--force-with-lease={provider_ref}:{expected_head}",
            remote,
            f":{provider_ref}",
        ],
    )
    after = remote_provider_ref_snapshot(repo_root, remote=remote, provider_ref=provider_ref)
    tracking_cleanup = run_git(repo_root, ["update-ref", "-d", f"refs/remotes/{remote}/{branch}"]) if after["status"] == "absent" else None
    absent_after_authoritative_query = after["status"] == "absent" and tracking_cleanup is not None and tracking_cleanup.returncode == 0
    action = {
        "status": "success" if absent_after_authoritative_query else "blocked",
        "action": "delete_remote_branch",
        "remote": remote,
        "branch": branch,
        "expectedHead": expected_head,
        "returncode": delete.returncode,
        "providerQueryBefore": before,
        "providerQueryAfter": after,
        "remoteTrackingHeadAfter": rev_parse(repo_root, f"refs/remotes/{remote}/{branch}", required=False),
        "trackingCleanupReturncode": tracking_cleanup.returncode if tracking_cleanup is not None else None,
        "alreadyMissingAfterAttempt": delete.returncode != 0 and absent_after_authoritative_query,
        "stdout": delete.stdout[-2000:],
        "stderr": delete.stderr[-2000:],
    }
    if action["status"] != "success":
        action["reason"] = after.get("reason") or (
            "remote_tracking_ref_cleanup_failed"
            if after["status"] == "absent"
            else "remote_feature_still_present_after_delete"
        )
    write_audit(repo_root, config, "remote_branch_deletion", action, outcome="success" if action["status"] == "success" else "blocked")
    return action


def remote_feature_prune_still_eligible(repo_root: Path, item: Dict[str, Any], target_head: str) -> bool:
    head = str(item["head"])
    if is_ancestor(repo_root, head, target_head):
        return True
    if tree_hash(repo_root, head) == tree_hash(repo_root, target_head):
        return True
    if item.get("disposition") == "prune_patch_equivalent_remote_feature":
        cherry = cherry_rows_between(repo_root, target_head, head)
        return bool(cherry) and all(line.startswith("-") for line in cherry)
    return False


def apply_remote_feature_prune(
    repo_root: Path,
    config: Dict[str, Any],
    plan: Dict[str, Any],
    item: Dict[str, Any],
    *,
    recovery_artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    target = target_ref_for(repo_root, config)
    if target["head"] != plan["pinnedRefs"]["target"]["head"]:
        action = {"status": "blocked", "reason": "target_head_drifted", "expected": plan["pinnedRefs"]["target"], "actual": target}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    remote = str(item["remote"])
    branch = str(item["branch"])
    current_head = rev_parse(repo_root, str(item["ref"]), required=False)
    if current_head != item["head"]:
        action = {"status": "blocked", "reason": "remote_feature_head_drifted", "remote": remote, "branch": branch, "expected": item["head"], "actual": current_head}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    if not remote_feature_prune_still_eligible(repo_root, item, str(target["head"])):
        action = {"status": "blocked", "reason": "remote_feature_no_longer_prunable", "remote": remote, "branch": branch, "head": item["head"], "target": target}
        write_audit(repo_root, config, "cleanup_retention", action, outcome="retained")
        return action
    recovery_verification = verify_prune_recovery_artifact(
        repo_root,
        config,
        recovery_artifact,
        candidate_id=str((recovery_artifact or {}).get("candidateId") or ""),
        branch_head=str(item["head"]),
        target_head=str(plan["pinnedRefs"]["target"]["head"]),
    )
    if not recovery_verification["ok"]:
        action = {
            "status": "blocked",
            "reason": "remote_prune_recovery_artifact_invalid",
            "remote": remote,
            "branch": branch,
            "recoveryArtifact": recovery_artifact,
            "recoveryVerification": recovery_verification,
        }
        write_audit(repo_root, config, "manual_prune_recovery", action, outcome="blocked")
        return action
    action = delete_remote_feature_ref(repo_root, config, remote=remote, branch=branch, expected_head=str(item["head"]))
    action["recoveryArtifact"] = recovery_artifact
    return action


def apply_remote_feature_clean_integrate(repo_root: Path, config: Dict[str, Any], plan: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    target = target_ref_for(repo_root, config)
    if target["head"] != plan["pinnedRefs"]["target"]["head"]:
        return {"status": "blocked", "reason": "target_head_drifted", "expected": plan["pinnedRefs"]["target"], "actual": target}
    remote = str(report.get("remote") or target.get("remote") or config.get("git", {}).get("remote", "origin"))
    branch = str(report["branch"])
    remote_ref = str(report.get("remoteRef") or f"{remote}/{branch}")
    branch_head = str(report["head"])
    current_head = rev_parse(repo_root, remote_ref, required=False)
    if current_head != branch_head:
        action = {"status": "blocked", "reason": "remote_feature_head_drifted", "remote": remote, "branch": branch, "expected": branch_head, "actual": current_head}
        write_audit(repo_root, config, "stale_refs", action, outcome="blocked")
        return action
    target_branch = str(plan["pinnedRefs"]["target"]["branch"])
    integration_path = closeout_state_root(repo_root, config) / "rs-remote-iw" / uuid.uuid4().hex[:12]
    integration_path.parent.mkdir(parents=True, exist_ok=True)
    add = run_git_longpaths(repo_root, ["worktree", "add", "--detach", str(integration_path), target["head"]])
    if add.returncode != 0:
        return {"status": "blocked", "reason": "integration_worktree_failed", "returncode": add.returncode, "stderr": add.stderr[-2000:]}
    try:
        merge = run_git(integration_path, ["merge", "--no-ff", "--no-edit", branch_head])
        if merge.returncode != 0:
            return {"status": "blocked", "reason": "merge_failed", "returncode": merge.returncode, "stdout": merge.stdout[-2000:], "stderr": merge.stderr[-2000:]}
        diff_check = run_git(integration_path, ["diff", "--check"])
        if diff_check.returncode != 0:
            return {"status": "blocked", "reason": "diff_check_failed", "returncode": diff_check.returncode, "stdout": diff_check.stdout[-2000:], "stderr": diff_check.stderr[-2000:]}
        validations = run_validations(repo_root, config, integration_path)
        failed = next((row for row in validations if row["returncode"] != 0), None)
        if failed:
            return {"status": "blocked", "reason": "validation_failed", "validations": validations}
        new_head = git_stdout(integration_path, ["rev-parse", "HEAD"])
        push_result: Optional[Dict[str, Any]] = None
        if target["mode"] == "remote":
            push = run_git(integration_path, ["push", str(target.get("remote")), f"HEAD:{target_branch}"])
            push_result = {"remote": target.get("remote"), "targetBranch": target_branch, "returncode": push.returncode, "stdout": push.stdout[-2000:], "stderr": push.stderr[-2000:]}
            if push.returncode != 0:
                return {"status": "blocked", "reason": "target_push_failed", "push": push_result}
        local_update = update_local_target(repo_root, target_branch, new_head)
        if local_update["returncode"] != 0:
            return {"status": "blocked", "reason": "local_target_update_failed", "localUpdate": local_update, "push": push_result}
        remote_cleanup: Optional[Dict[str, Any]] = None
        if bool(config.get("repoSweep", {}).get("deleteRemoteFeatureAfterCleanIntegrate", True)):
            remote_cleanup = delete_remote_feature_ref(repo_root, config, remote=remote, branch=branch, expected_head=branch_head)
        result = {
            "status": "success" if not remote_cleanup or remote_cleanup.get("status") == "success" else "blocked",
            "action": "clean_integrate_remote_feature",
            "remote": remote,
            "branch": branch,
            "newTargetHead": new_head,
            "validations": validations,
            "push": push_result,
            "localUpdate": local_update,
            "remoteCleanup": remote_cleanup,
        }
        write_audit(repo_root, config, "success" if result["status"] == "success" else "cleanup_retention", result, outcome="success" if result["status"] == "success" else "blocked")
        return result
    finally:
        removal = remove_worktree(repo_root, integration_path)
        write_audit(repo_root, config, "snapshot_pruning", {"action": "integration_worktree_remove", **removal}, outcome="success" if removal["returncode"] == 0 else "blocked")


def repo_sweep_clean_detached_worktree_candidate(config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    evidence = {
        "candidate": {
            "path": item.get("path"),
            "head": item.get("head"),
            "disposition": item.get("disposition"),
            "reason": item.get("reason"),
            "dirtyState": item.get("dirtyState"),
        },
        "target": plan["pinnedRefs"]["target"],
        "policy": {
            "pruneWorktrees": config.get("repoSweep", {}).get("pruneWorktrees"),
            "removeCleanDetachedWorktreesInSweep": config.get("cleanupPolicy", {}).get("removeCleanDetachedWorktreesInSweep"),
        },
    }
    return {
        "candidateId": "candidate:clean-detached-worktree:%s" % stable_hash(evidence, 16),
        "actionId": "remove_clean_detached_worktree",
        "actionClass": "clean_detached_worktree_prune",
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": {
            "target": plan["pinnedRefs"]["target"],
            "worktree": {
                "path": item.get("path"),
                "head": item.get("head"),
            },
        },
    }


def repo_sweep_stash_drop_candidate(config: Dict[str, Any], plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    evidence = {
        "candidate": {
            "ref": item.get("ref"),
            "head": item.get("head"),
            "message": item.get("message"),
            "disposition": item.get("disposition"),
            "reason": item.get("reason"),
        },
        "target": plan["pinnedRefs"]["target"],
        "policy": {
            "stashMode": config.get("repoSweep", {}).get("stashMode"),
        },
    }
    return {
        "candidateId": "candidate:stash-drop:%s" % stable_hash(evidence, 16),
        "actionId": "drop_stash",
        "actionClass": "stash_drop",
        "evidence": evidence,
        "evidenceHash": stable_hash(evidence),
        "pinnedRefs": {
            "target": plan["pinnedRefs"]["target"],
            "stash": {
                "ref": item.get("ref"),
                "head": item.get("head"),
            },
        },
    }


def repo_sweep_apply_tuple(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidateId": str(candidate.get("candidateId") or ""),
        "actionId": str(candidate.get("actionId") or ""),
        "evidenceHash": str(candidate.get("evidenceHash") or ""),
    }


def repo_sweep_apply_scope(
    config: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    branch_candidates: Sequence[Dict[str, Any]],
    remote_feature_candidates: Sequence[Dict[str, Any]],
    promoted_candidates: Sequence[Dict[str, Any]],
    candidate_worktrees: Sequence[Dict[str, Any]],
    candidate_stashes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    remove_clean_detached = (
        bool(config.get("cleanupPolicy", {}).get("removeCleanDetachedWorktreesInSweep", False))
        or str(config.get("repoSweep", {}).get("pruneWorktrees") or "") == "clean_detached_only"
    )
    drop_stashes = bool(config.get("cleanupPolicy", {}).get("dropStashesInSweep", False))
    worktree_candidates = [
        repo_sweep_clean_detached_worktree_candidate(config, plan, item)
        for item in candidate_worktrees
    ] if remove_clean_detached else []
    stash_candidates = [
        repo_sweep_stash_drop_candidate(config, plan, item)
        for item in candidate_stashes
    ] if drop_stashes else []
    candidates = [*branch_candidates, *remote_feature_candidates, *promoted_candidates, *worktree_candidates, *stash_candidates]
    return {
        "candidateCount": len(candidates),
        "candidateIds": [str(candidate.get("candidateId")) for candidate in candidates],
        "candidateTuples": [repo_sweep_apply_tuple(candidate) for candidate in candidates],
        "worktreeCandidates": worktree_candidates,
        "stashCandidates": stash_candidates,
    }


def validate_repo_sweep_bulk_override(
    config: Dict[str, Any],
    apply_scope: Dict[str, Any],
    bulk_override: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    candidate_tuples = list(apply_scope.get("candidateTuples") or [])
    if len(candidate_tuples) <= 1:
        return {"ok": True, "required": False, "reason": "single_or_zero_candidate", "candidateCount": len(candidate_tuples)}
    if not bulk_override:
        return {
            "ok": False,
            "required": True,
            "reason": "repo_sweep_bulk_override_required",
            "candidateCount": len(candidate_tuples),
            "candidateIds": apply_scope.get("candidateIds") or [],
            "expectedTuples": candidate_tuples,
            "recoveryCommand": "Rerun repo sweep with --candidate-id for one candidate, run retained remediation, or provide an audited bulk override file.",
        }
    reasons: List[str] = []
    bulk_config = config.get("repoSweep", {}).get("auditedBulkOverride", {})
    if not bool(bulk_config.get("enabled", False)):
        reasons.append("repoSweep.auditedBulkOverride.enabled_false")
    if bulk_override.get("enabled") is not True:
        reasons.append("override.enabled_not_true")
    for field in ["reason", "approvedBy", "recoveryCommand"]:
        if not str(bulk_override.get(field) or "").strip():
            reasons.append(f"override.{field}_missing")
    reviewer_approval = bulk_override.get("reviewerApproval") or {}
    if reviewer_approval.get("approved") is not True:
        reasons.append("override.reviewerApproval.approved_not_true")
    if not str(reviewer_approval.get("reviewer") or "").strip():
        reasons.append("override.reviewerApproval.reviewer_missing")
    expected_ids = sorted(str(item.get("candidateId") or "") for item in candidate_tuples)
    provided_ids = sorted(str(item) for item in (bulk_override.get("candidateIds") or []))
    if provided_ids != expected_ids:
        reasons.append("override.candidateIds_mismatch")
    expected_tuples = sorted(candidate_tuples, key=lambda item: (item["candidateId"], item["actionId"], item["evidenceHash"]))
    provided_tuples = sorted(
        [repo_sweep_apply_tuple(item) for item in (bulk_override.get("perCandidateTuples") or [])],
        key=lambda item: (item["candidateId"], item["actionId"], item["evidenceHash"]),
    )
    if provided_tuples != expected_tuples:
        reasons.append("override.perCandidateTuples_mismatch")
    result = {
        "ok": not reasons,
        "required": True,
        "reason": "audited_bulk_override_accepted" if not reasons else "audited_bulk_override_invalid",
        "reasons": reasons,
        "candidateCount": len(candidate_tuples),
        "candidateIds": expected_ids,
        "expectedTuples": expected_tuples,
        "providedTuples": provided_tuples,
        "override": {
            "reason": bulk_override.get("reason"),
            "approvedBy": bulk_override.get("approvedBy"),
            "reviewerApproval": reviewer_approval,
            "recoveryCommand": bulk_override.get("recoveryCommand"),
        },
    }
    if reasons:
        result["recoveryCommand"] = "Fix the audited bulk override file or rerun with --candidate-id for one candidate."
    return result


def repo_sweep(repo_root_arg: Path, *, apply: bool = False, candidate_id: Optional[str] = None, bulk_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="repo-sweep", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    tooling = verify_closeout_tooling_current(repo_root, config, plan_only=True)
    if not tooling["ok"]:
        return {"status": "blocked", "reason": "closeout_tooling_stale", "tooling": tooling}
    if not bool(config.get("repoSweep", {}).get("enabled", True)):
        return {"status": "disabled", "reason": "repoSweep.enabled is false"}
    plan = repo_sweep_plan(repo_root, config)
    tuple_info = repo_sweep_tuple(repo_root, plan=plan, config=config)
    backup_patterns = config.get("repoSweep", {}).get("backupBranchPatterns", [])
    prunable_branches = [
        item
        for item in plan["branchPlans"]
        if item["disposition"] == "prune_merged_branch" and not path_matches_any(str(item["branch"]), backup_patterns)
        and not is_recovery_branch(config, str(item["branch"]))
    ]
    prunable_remote_features = [
        item
        for item in plan.get("remoteFeaturePlans", [])
        if item["disposition"] in {"prune_integrated_remote_feature", "prune_patch_equivalent_remote_feature"}
    ]
    candidate_worktrees = [item for item in plan["worktreePlans"] if item["disposition"] == "candidate_clean_detached_worktree_prune"]
    candidate_stashes = [item for item in plan["stashPlans"] if item["disposition"] == "candidate_stash_drop"]
    branch_candidates = [branch_prune_candidate(config, plan, item) for item in prunable_branches]
    remote_feature_candidates = [remote_feature_prune_candidate(config, plan, item) for item in prunable_remote_features]
    investigation_progress = investigation_reports(repo_root, config, plan)
    reused_candidate_ids = set(investigation_progress.get("reusedCandidateIds") or [])
    if apply and investigation_progress["complete"]:
        reused_promoted_pairs = []
        for cached_report in investigation_progress["reports"]:
            if cached_report.get("candidateId") not in reused_candidate_ids:
                continue
            if cached_report.get("recommendedAction") not in {
                "clean_integrate_now",
                "clean_integrate_remote_now",
                "dispatch_conflict_remediation",
                "prune_now",
                "prune_remote_now",
                "cleanup_worktree_and_prune",
                "split_now",
                "switch_target_and_prune",
                "preserve_detached_dirty_now",
                "prune_generated_detached_dirty_now",
            }:
                continue
            cached_candidate = candidate_from_report(config, plan, cached_report)
            reused_promoted_pairs.append((cached_report, cached_candidate))
        max_fresh = positive_int(config.get("repoSweep", {}).get("maxNewInvestigationsPerRun"), 2)
        if not candidate_id and len(reused_promoted_pairs) > max_fresh:
            result = {
                "status": "blocked",
                "reason": "repo_sweep_cached_investigations_require_exact_candidate",
                "recoveryCommand": "Rerun repo sweep with --candidate-id for one promoted candidate so its recommendation is freshly recomputed.",
                "candidateIds": [candidate["candidateId"] for _, candidate in reused_promoted_pairs],
                "investigationProgress": investigation_progress,
                "plan": plan,
                "tuple": tuple_info,
            }
            write_audit(repo_root, config, "cleanup_retention", result, outcome="blocked")
            return result
        selected_reused_pairs = (
            reused_promoted_pairs
            if not candidate_id
            else [
                (report, candidate)
                for report, candidate in reused_promoted_pairs
                if repo_sweep_candidate_filter_matches(candidate_id, candidate, report)
            ]
        )
        if candidate_id and len(selected_reused_pairs) > 1:
            result = {
                "status": "blocked",
                "reason": "repo_sweep_cached_candidate_selection_is_ambiguous",
                "candidateId": candidate_id,
                "matchedReportIds": [report["candidateId"] for report, _ in selected_reused_pairs],
                "plan": plan,
                "tuple": tuple_info,
            }
            write_audit(repo_root, config, "cleanup_retention", result, outcome="blocked")
            return result
        if selected_reused_pairs:
            fresh_reports: Dict[str, Dict[str, Any]] = {}
            for cached_report, _ in selected_reused_pairs:
                fresh_reports[str(cached_report["candidateId"])] = fresh_investigation_report(
                    repo_root,
                    config,
                    plan,
                    str(cached_report["candidateId"]),
                )
            investigation_progress["reports"] = [
                fresh_reports.get(str(report.get("candidateId")), report)
                for report in investigation_progress["reports"]
            ]
            investigation_progress["revalidatedCandidateIds"] = sorted(fresh_reports)
    retained_reports = investigation_progress["reports"]
    promoted_reports = [
        report
        for report in retained_reports
        if report.get("recommendedAction")
        in {"clean_integrate_now", "clean_integrate_remote_now", "dispatch_conflict_remediation", "prune_now", "prune_remote_now", "cleanup_worktree_and_prune", "split_now", "switch_target_and_prune", "preserve_detached_dirty_now", "prune_generated_detached_dirty_now"}
    ]
    promoted_candidates = [candidate_from_report(config, plan, report) for report in promoted_reports]
    follow_up_candidates = retained_reports
    remove_clean_detached = (
        bool(config.get("cleanupPolicy", {}).get("removeCleanDetachedWorktreesInSweep", False))
        or str(config.get("repoSweep", {}).get("pruneWorktrees") or "") == "clean_detached_only"
    )
    drop_stashes = bool(config.get("cleanupPolicy", {}).get("dropStashesInSweep", False))
    apply_scope = repo_sweep_apply_scope(
        config,
        plan,
        branch_candidates=branch_candidates,
        remote_feature_candidates=remote_feature_candidates,
        promoted_candidates=promoted_candidates,
        candidate_worktrees=candidate_worktrees,
        candidate_stashes=candidate_stashes,
    )
    def candidate_filter_matches(candidate: Dict[str, Any], report: Optional[Dict[str, Any]] = None) -> bool:
        return repo_sweep_candidate_filter_matches(candidate_id, candidate, report)

    if not apply:
        payload = {
            "plan": plan,
            "tuple": tuple_info,
            "branchCandidates": branch_candidates,
            "remoteFeatureCandidates": remote_feature_candidates,
            "retainedCandidateReports": retained_reports,
            "promotedCandidates": promoted_candidates,
            "followUpCandidates": follow_up_candidates,
            "applyScope": apply_scope,
            "investigationProgress": investigation_progress,
        }
        write_audit(repo_root, config, "cleanup_retention", payload, outcome="recorded")
        return {
            "status": "planned" if investigation_progress["complete"] else "planned_partial",
            "recoveryCommand": None if investigation_progress["complete"] else closeout_script_command("repo-sweep-closeout.ps1", config=config),
            **payload,
        }
    if not investigation_progress["complete"]:
        result = {
            "status": "blocked",
            "reason": "repo_sweep_investigations_pending",
            "recoveryCommand": closeout_script_command("repo-sweep-closeout.ps1", config=config),
            "investigationProgress": investigation_progress,
            "plan": plan,
            "tuple": tuple_info,
        }
        write_audit(repo_root, config, "cleanup_retention", result, outcome="blocked")
        return result
    if not candidate_id:
        bulk_check = validate_repo_sweep_bulk_override(config, apply_scope, bulk_override)
        if not bulk_check["ok"]:
            result = {
                "status": "blocked",
                "reason": bulk_check["reason"],
                "bulkOverride": bulk_check,
                "plan": plan,
                "tuple": tuple_info,
                "branchCandidates": branch_candidates,
                "remoteFeatureCandidates": remote_feature_candidates,
                "retainedCandidateReports": retained_reports,
                "promotedCandidates": promoted_candidates,
                "followUpCandidates": follow_up_candidates,
                "applyScope": apply_scope,
            }
            write_audit(repo_root, config, "repo_sweep_bulk_override_required", result, outcome="blocked")
            return result
        if bulk_check.get("required"):
            write_audit(repo_root, config, "repo_sweep_audited_bulk_override", bulk_check, outcome="recorded")
    actions: List[Dict[str, Any]] = []
    quorum_results: List[Dict[str, Any]] = []
    matched_candidate = candidate_id is None
    if bool(config.get("repoSweep", {}).get("pruneMergedLocalBranches", True)):
        for item, candidate in zip(prunable_branches, branch_candidates):
            if candidate_id and not candidate_filter_matches(candidate):
                continue
            matched_candidate = True
            current_head = rev_parse(repo_root, f"refs/heads/{item['branch']}", required=False)
            blockers: List[str] = []
            if current_head != item["head"]:
                blockers.append("branch_head_drifted")
            if not is_ancestor(repo_root, str(item["head"]), str(plan["pinnedRefs"]["target"]["head"])):
                blockers.append("branch_not_ancestor_of_target")
            recovery_artifact = write_evidence_preserving_prune_recovery(
                repo_root,
                config,
                candidate_id=str(candidate["candidateId"]),
                action_class=str(candidate["actionClass"]),
                branch=str(item["branch"]),
                head=str(item["head"]),
                target_head=str(plan["pinnedRefs"]["target"]["head"]),
                ref=f"refs/heads/{item['branch']}",
                reason=str(item.get("reason") or ""),
                source="local-branch",
                item=item,
            )
            if recovery_artifact.get("status") == "blocked":
                blockers.append(str(recovery_artifact.get("reason") or "prune_recovery_failed"))
            candidate = attach_prune_recovery_to_candidate(candidate, recovery_artifact)
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=blockers,
            )
            quorum_results.append({"candidate": candidate, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, **quorum_result}, outcome="blocked")
                continue
            actions.extend(cleanup_branch_after_sweep_action(repo_root, config, plan, item, recovery_artifact=recovery_artifact))
    if bool(config.get("repoSweep", {}).get("pruneRemoteFeatureBranches", True)):
        for item, candidate in zip(prunable_remote_features, remote_feature_candidates):
            if candidate_id and not candidate_filter_matches(candidate):
                continue
            matched_candidate = True
            blockers: List[str] = []
            current_head = rev_parse(repo_root, str(item["ref"]), required=False)
            current_target = target_ref_for(repo_root, config)
            if current_head != item["head"]:
                blockers.append("remote_feature_head_drifted")
            if current_target["head"] != plan["pinnedRefs"]["target"]["head"]:
                blockers.append("target_head_drifted")
            if not blockers and not remote_feature_prune_still_eligible(repo_root, item, str(plan["pinnedRefs"]["target"]["head"])):
                blockers.append("remote_feature_no_longer_prunable")
            recovery_artifact = write_evidence_preserving_prune_recovery(
                repo_root,
                config,
                candidate_id=str(candidate["candidateId"]),
                action_class=str(candidate["actionClass"]),
                branch=str(item["branch"]),
                head=str(item["head"]),
                target_head=str(plan["pinnedRefs"]["target"]["head"]),
                ref=str(item["ref"]),
                reason=str(item.get("reason") or ""),
                source="remote-feature",
                item=item,
            )
            if recovery_artifact.get("status") == "blocked":
                blockers.append(str(recovery_artifact.get("reason") or "prune_recovery_failed"))
            candidate = attach_prune_recovery_to_candidate(candidate, recovery_artifact)
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=blockers,
            )
            quorum_results.append({"candidate": candidate, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, **quorum_result}, outcome="blocked")
                continue
            action = apply_remote_feature_prune(repo_root, config, plan, item, recovery_artifact=recovery_artifact)
            actions.append(action)
    branch_by_name = {item["branch"]: item for item in plan["branchPlans"]}
    for report, candidate in zip(promoted_reports, promoted_candidates):
        if candidate_id and not candidate_filter_matches(candidate, report):
            continue
        matched_candidate = True
        if report["recommendedAction"] == "preserve_detached_dirty_now":
            current_paths = worktree_dirty_state(Path(str((report.get("worktree") or {}).get("path") or ""))).get("paths", [])
            blockers = list(report.get("blockers") or [])
            if sorted(current_paths) != sorted(report.get("scope", {}).get("dirtyPaths") or []):
                blockers.append("detached_dirty_tuple_drifted")
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=blockers,
            )
            quorum_results.append({"candidate": candidate, "report": report, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, "report": report, **quorum_result}, outcome="blocked")
                continue
            action = apply_detached_dirty_preserve(repo_root, config, plan, report)
            actions.append(action)
            continue
        if report["recommendedAction"] == "prune_generated_detached_dirty_now":
            worktree_path = Path(str((report.get("worktree") or {}).get("path") or ""))
            current_entries = parse_status_paths(worktree_path) if worktree_path.exists() else []
            current_paths = sorted({str(item["path"]) for item in current_entries})
            generated_only_proof = generated_only_detached_dirty_proof(config, current_entries)
            blockers = list(report.get("blockers") or [])
            if current_paths != sorted(report.get("scope", {}).get("dirtyPaths") or []):
                blockers.append("generated_only_detached_dirty_tuple_drifted")
            if not generated_only_proof["eligible"]:
                blockers.append("generated_only_detached_dirty_unmatched_paths")
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=blockers,
            )
            quorum_results.append({"candidate": candidate, "report": report, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, "report": report, **quorum_result}, outcome="blocked")
                continue
            action = apply_generated_only_detached_dirty_prune(repo_root, config, report)
            actions.append(action)
            continue
        if report["recommendedAction"] in {"clean_integrate_remote_now", "prune_remote_now"}:
            blockers = list(report.get("blockers") or [])
            current_target = target_ref_for(repo_root, config)
            current_remote_head = rev_parse(repo_root, str(report.get("remoteRef") or ""), required=False)
            if current_remote_head != report["head"]:
                blockers.append("remote_feature_head_drifted")
            if current_target["head"] != plan["pinnedRefs"]["target"]["head"]:
                blockers.append("target_head_drifted")
            recovery_artifact = None
            if report["recommendedAction"] == "prune_remote_now":
                recovery_artifact = write_evidence_preserving_prune_recovery(
                    repo_root,
                    config,
                    candidate_id=str(candidate["candidateId"]),
                    action_class=str(candidate["actionClass"]),
                    branch=str(report.get("branch")),
                    head=str(report.get("head")),
                    target_head=str(plan["pinnedRefs"]["target"]["head"]),
                    ref=str(report.get("remoteRef") or ""),
                    reason=str(report.get("reason") or ""),
                    source="remote-feature",
                    report=report,
                )
                if recovery_artifact.get("status") == "blocked":
                    blockers.append(str(recovery_artifact.get("reason") or "prune_recovery_failed"))
                candidate = attach_prune_recovery_to_candidate(candidate, recovery_artifact)
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=blockers,
            )
            quorum_results.append({"candidate": candidate, "report": report, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, "report": report, **quorum_result}, outcome="blocked")
                continue
            if report["recommendedAction"] == "clean_integrate_remote_now":
                action = apply_remote_feature_clean_integrate(repo_root, config, plan, report)
            else:
                remote_item = {
                    "remote": report.get("remote"),
                    "branch": report.get("branch"),
                    "ref": report.get("remoteRef"),
                    "head": report.get("head"),
                    "disposition": "prune_patch_equivalent_remote_feature",
                }
                action = apply_remote_feature_prune(repo_root, config, plan, remote_item, recovery_artifact=recovery_artifact)
            actions.append(action)
            continue
        if report["recommendedAction"] == "dispatch_conflict_remediation":
            quorum_result = ensure_autonomous_quorum(
                repo_root,
                config,
                candidate_id=candidate["candidateId"],
                action_id=candidate["actionId"],
                evidence_hash=candidate["evidenceHash"],
                pinned_refs=candidate["pinnedRefs"],
                evidence=candidate["evidence"],
                action_class=candidate["actionClass"],
                blockers=list(report.get("blockers") or []),
            )
            quorum_results.append({"candidate": candidate, "report": report, **quorum_result})
            if not quorum_result["quorum"]["ok"]:
                write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, "report": report, **quorum_result}, outcome="blocked")
                continue
            action = dispatch_agent_conflict_remediation(repo_root, config, plan, report, candidate)
            actions.append(action)
            continue
        item = branch_by_name.get(report["branch"])
        if not item:
            continue
        current_head = rev_parse(repo_root, f"refs/heads/{report['branch']}", required=False)
        blockers = list(report.get("blockers") or [])
        if current_head != report["head"]:
            blockers.append("branch_head_drifted")
        current_target = target_ref_for(repo_root, config)
        if current_target["head"] != plan["pinnedRefs"]["target"]["head"]:
            blockers.append("target_head_drifted")
        recovery_artifact = None
        if report["recommendedAction"] in {"prune_now", "cleanup_worktree_and_prune"}:
            recovery_artifact = write_evidence_preserving_prune_recovery(
                repo_root,
                config,
                candidate_id=str(candidate["candidateId"]),
                action_class=str(candidate["actionClass"]),
                branch=str(report.get("branch")),
                head=str(report.get("head")),
                target_head=str(plan["pinnedRefs"]["target"]["head"]),
                ref=f"refs/heads/{report.get('branch')}",
                reason=str(report.get("reason") or ""),
                source="local-branch",
                item=item,
                report=report,
            )
            if recovery_artifact.get("status") == "blocked":
                blockers.append(str(recovery_artifact.get("reason") or "prune_recovery_failed"))
            candidate = attach_prune_recovery_to_candidate(candidate, recovery_artifact)
        quorum_result = ensure_autonomous_quorum(
            repo_root,
            config,
            candidate_id=candidate["candidateId"],
            action_id=candidate["actionId"],
            evidence_hash=candidate["evidenceHash"],
            pinned_refs=candidate["pinnedRefs"],
            evidence=candidate["evidence"],
            action_class=candidate["actionClass"],
            blockers=blockers,
        )
        quorum_results.append({"candidate": candidate, "report": report, **quorum_result})
        if not quorum_result["quorum"]["ok"]:
            write_audit(repo_root, config, "review_quorum_blocked", {"candidate": candidate, "report": report, **quorum_result}, outcome="blocked")
            continue
        if report["recommendedAction"] == "clean_integrate_now":
            action = apply_repo_sweep_clean_integrate(repo_root, config, plan, item)
            actions.append(action)
            write_audit(repo_root, config, "success" if action.get("status") == "success" else "blocked_repair", {"candidate": candidate, "report": report, "action": action}, outcome="success" if action.get("status") == "success" else "blocked")
        elif report["recommendedAction"] == "switch_target_and_prune":
            action = cleanup_foreign_dirty_integrated_branch(repo_root, config, plan, item, report)
            actions.append(action)
        elif report["recommendedAction"] == "split_now":
            action = preserve_owned_dirty_split(repo_root, work_block_id=report.get("dirtyClassification", {}).get("workBlockId"))
            actions.append(action)
            write_audit(repo_root, config, "dirty_split_success" if action.get("status") == "success" else "dirty_split_blocked_preservation", {"candidate": candidate, "report": report, "action": action}, outcome="success" if action.get("status") == "success" else "blocked")
        elif report["recommendedAction"] in {"prune_now", "cleanup_worktree_and_prune"}:
            actions.extend(
                cleanup_branch_after_sweep_action(
                    repo_root,
                    config,
                    plan,
                    item,
                    force_branch_delete=report.get("actionClass") in {"redundant_backup_prune", "redundant_branch_prune"},
                    allow_protected_worktree=report.get("actionClass") == "explicit_protected_worktree_cleanup",
                    recovery_artifact=recovery_artifact,
                )
            )
    if remove_clean_detached:
        for item, candidate in zip(candidate_worktrees, apply_scope["worktreeCandidates"]):
            if candidate_id and not candidate_filter_matches(candidate):
                continue
            matched_candidate = True
            action = {"action": "remove_clean_detached_worktree", **remove_worktree(repo_root, Path(str(item["path"])))}
            actions.append(action)
            write_audit(repo_root, config, "snapshot_pruning", action, outcome="success" if action["returncode"] == 0 else "blocked")
    if drop_stashes:
        for item, candidate in zip(candidate_stashes, apply_scope["stashCandidates"]):
            if candidate_id and not candidate_filter_matches(candidate):
                continue
            matched_candidate = True
            drop = run_git(repo_root, ["stash", "drop", item["ref"]])
            action = {"action": "drop_stash", "stash": item["ref"], "returncode": drop.returncode, "stderr": drop.stderr[-2000:]}
            actions.append(action)
            write_audit(repo_root, config, "snapshot_pruning", action, outcome="success" if drop.returncode == 0 else "blocked")
    if candidate_id and not matched_candidate:
        result = {
            "status": "blocked",
            "reason": "candidate_not_found_or_not_promoted",
            "candidateId": candidate_id,
            "branchCandidates": branch_candidates,
            "remoteFeatureCandidates": remote_feature_candidates,
            "promotedCandidates": promoted_candidates,
        }
        write_audit(repo_root, config, "blocked_repair", result, outcome="blocked")
        return result
    blocked_quorums = [item for item in quorum_results if not item.get("quorum", {}).get("ok")]
    blocked_actions = [
        item
        for item in actions
        if item.get("status") == "blocked"
        or item.get("returncode") not in (None, 0)
        or item.get("pruneReturncode") not in (None, 0)
    ]
    status = "blocked" if blocked_quorums or blocked_actions else "success"
    reason = None
    if blocked_quorums:
        reason = "review_quorum_blocked"
    elif blocked_actions:
        reason = "repo_sweep_action_blocked"
    post_prune_sweep: Optional[Dict[str, Any]] = None
    if status == "success" and evidence_preserving_prune_config(config)["rerunSweepAfterPrune"]:
        mutated_prune = any(
            item.get("action")
            in {
                "delete_local_branch",
                "delete_remote_branch",
                "remove_branch_worktree",
                "remove_clean_detached_worktree",
                "detached_dirty_preserve",
                "generated_only_detached_dirty_prune",
                "foreign_dirty_integrated_branch_prune",
            }
            for item in actions
        )
        if mutated_prune:
            refreshed = repo_sweep_plan(repo_root, config)
            post_prune_sweep = {
                "evidenceHash": refreshed["evidenceHash"],
                "branchCandidateCount": len([item for item in refreshed["branchPlans"] if item["disposition"] not in {"retain_protected_branch"}]),
                "remoteFeatureCandidateCount": len(refreshed.get("remoteFeaturePlans", [])),
                "stashCandidateCount": len(refreshed.get("stashPlans", [])),
                "worktreeCandidateCount": len([item for item in refreshed.get("worktreePlans", []) if item.get("disposition") != "retain_branch_worktree"]),
            }
    result = {
        "status": status,
        "reason": reason,
        "candidateId": candidate_id,
        "plan": plan,
        "tuple": tuple_info,
        "branchCandidates": branch_candidates,
        "remoteFeatureCandidates": remote_feature_candidates,
        "retainedCandidateReports": retained_reports,
        "promotedCandidates": promoted_candidates,
        "followUpCandidates": follow_up_candidates,
        "applyScope": apply_scope,
        "quorumResults": quorum_results,
        "actions": actions,
        "postPruneSweep": post_prune_sweep,
    }
    write_audit(repo_root, config, "success" if status == "success" else "blocked_repair", result, outcome=status)
    return result


def ordered_retained_remediation_candidates(planned: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    candidates.extend(planned.get("branchCandidates") or [])
    candidates.extend(planned.get("remoteFeatureCandidates") or [])
    candidates.extend(planned.get("promotedCandidates") or [])
    return [item for item in candidates if item.get("candidateId")]


def remediate_retained_candidates(repo_root_arg: Path, *, apply: bool = False, candidate_id: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    freeze = remediation_freeze_status(repo_root, config, action="retained-remediation", write_audit_packet=True)
    if freeze["active"]:
        return {"status": "blocked", "reason": "remediation_freeze", "freeze": freeze}
    if not apply:
        planned = repo_sweep(repo_root, apply=False, candidate_id=candidate_id)
        result = {"actor": "retained-remediation", **planned}
        write_audit(repo_root, config, "retained_remediation", {"apply": False, "candidateId": candidate_id, "resultStatus": planned.get("status")}, outcome="recorded")
        return result
    if candidate_id:
        applied = repo_sweep(repo_root, apply=True, candidate_id=candidate_id)
        result = {"actor": "retained-remediation", "selectedCandidateId": candidate_id, **applied}
        write_audit(repo_root, config, "retained_remediation", {"apply": True, "candidateId": candidate_id, "resultStatus": applied.get("status")}, outcome="success" if applied.get("status") == "success" else "blocked")
        return result
    planned = repo_sweep(repo_root, apply=False)
    candidates = ordered_retained_remediation_candidates(planned)
    if not candidates:
        result = {**planned, "actor": "retained-remediation", "status": "success", "reason": "no_promoted_candidates"}
        write_audit(repo_root, config, "retained_remediation", {"apply": True, "candidateId": None, "resultStatus": "no_promoted_candidates"}, outcome="success")
        return result
    selected = str(candidates[0]["candidateId"])
    applied = repo_sweep(repo_root, apply=True, candidate_id=selected)
    result = {"actor": "retained-remediation", "selectedCandidateId": selected, "plannedCandidateCount": len(candidates), **applied}
    write_audit(repo_root, config, "retained_remediation", {"apply": True, "candidateId": selected, "candidateCount": len(candidates), "resultStatus": applied.get("status")}, outcome="success" if applied.get("status") == "success" else "blocked")
    return result


def audit_summary(repo_root_arg: Path, *, limit: int = 20) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    rows: List[Dict[str, Any]] = []
    path = audit_root(repo_root, config) / "audits.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return {"status": "success", "auditCount": len(rows), "audits": rows[-limit:]}


def broker_contract(repo_root_arg: Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg)
    config = load_closeout_config(repo_root)
    scripts_dir = repo_root / "tools" / "closeout"
    scripts = sorted(path.name for path in scripts_dir.glob("*.ps1")) if scripts_dir.exists() else []
    required_config_keys = [
        "git",
        "workBlockBootstrap",
        "workBlockBootstrap.requireIntegratedStartHeadForFinalize",
        "contentReviewGate",
        "contentReviewGate.requireClaudeApprovalForFinalize",
        "contentReviewGate.coordinationFile",
        "contentReviewGate.handoffActor",
        "contentReviewGate.reviewActor",
        "contentReviewGate.authorizedReviewSessions",
        "validation",
        "processResources",
        "powerShell",
        "powerShell.preferredExecutable",
        "powerShell.fallbackExecutable",
        "powerShell.requiredArgs",
        "powerShell.preferFor",
        "powerShell.fallbackOnlyWhenPwshUnavailable",
        "powerShell.windowsPowerShellOnly",
        "powerShell.justification",
        "paths",
        "dirty.cleanAtStartSensitiveAutoClaim",
        "dirtySplit",
        "toolingBaseline",
        "evidenceRepair",
        "stashPolicy",
        "cleanupPolicy",
        "reviewQuorum",
        "responseHookLifecycle",
        "hardClean",
        "runtimeServices",
        "blockerAutoRemediation",
        "closeoutAddendumPersistence",
        "finalizeLoop",
        "remediationFreeze",
        "agentRemediation",
        "agentRemediationQueue",
        "locking",
        "autoEligibilityRepair",
        "repoStateLedger",
        "webDashboardSpec",
        "webDashboardSpec.readOnlyByDefault",
        "webDashboardSpec.preserveClientStateAcrossRefresh",
        "webDashboardSpec.rollbackForbiddenActions",
        "rollbackPolicy",
    ]
    return {
        "schemaVersion": BROKER_SCHEMA_VERSION,
        "configPath": str(repo_root / CONFIG_PATH),
        "policyHash": config.get("policyHash"),
        "requiredConfigKeys": required_config_keys,
        "missingConfigKeys": [key for key in required_config_keys if config_path_value(config, key) is None],
        "requiredScripts": REQUIRED_SCRIPT_NAMES,
        "scripts": scripts,
        "missingScripts": [name for name in REQUIRED_SCRIPT_NAMES if name not in scripts],
        "highImpactActions": HIGH_IMPACT_ACTIONS,
    }
