from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0"
POLICY_VERSION = "2026-05-03.v3"
POLICY_DOC_PATH = Path("tools/repo-hygiene/POLICY.md")
CONFIG_PATH = Path("tools/repo-hygiene/hygiene.config.json")
CONTRACT_PATH = Path("tools/repo-hygiene/closeout.contract.json")

IMPLEMENTED_RISK_TIERS = ["R0", "R1", "R2", "R3", "R4", "R5"]
IMPLEMENTED_CANDIDATE_KINDS = [
    "generated-report",
    "dirty-group",
    "branch",
    "worktree",
    "orphan-dir",
    "stash",
]
IMPLEMENTED_ACTION_IDS = [
    "retain",
    "ask",
    "commit",
    "split",
    "stash",
    "ignore-generated",
    "repo_hygiene_prune_old_runs",
    "branch_archive_delete",
    "worktree_remove",
    "orphan_quarantine",
    "stash_promote",
]
IMPLEMENTED_DASHBOARD_ACTION_IDS = ["repo_hygiene_prune_old_runs"]
IMPLEMENTED_CLOSEOUT_CANDIDATE_KINDS = [
    "closeout-transaction",
    "commit-unit",
    "merge-readiness",
    "publish-target",
    "prune-after-publish",
    "repo-sweep-retained-blocker",
    "detached-dirty-worktree",
    "protected-worktree-cleanup",
    "remote-feature-branch",
]
IMPLEMENTED_CLOSEOUT_ACTION_IDS = [
    "commit_unit_commit",
    "publish_pr",
    "publish_direct_branch",
    "local_merge",
    "prune_after_publish",
    "split",
    "delete_remote_branch",
    "remote_feature_clean_integrate",
    "remote_feature_prune",
    "foreign_dirty_integrated_branch_prune",
    "detached_dirty_preserve",
    "explicit_protected_worktree_cleanup",
    "resolve_conflicts_with_agent",
    "agent_remediation_surface_unavailable",
    "protected-target-noop-closeout",
    "evidence_preserving_prune_recovery",
]
IMPLEMENTED_CLOSEOUT_PUBLISH_MODES = ["pr_only", "direct_push_branch", "local_merge_only", "no_publish"]
IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS = [
    "dirty_current_work",
    "dirty_generated_only",
    "clean_feature_branch_ready_to_publish",
    "hygiene_cleanup_recommendations",
    "retained_blocker_auto_remediation",
    "agent_remediation_queue_consumer",
    "protected_target_noop_closeout",
]
IMPLEMENTED_CLOSEOUT_ARTIFACT_NAMES = [
    "decision-packet.json",
    "codex-closeout-recommendation.json",
    "agent-review-*.json",
    "approval.json",
    "approval-anchor.json",
    "apply-validation.json",
    "executor-handoff.json",
    "closeout-compare-result.json",
    "closeout-compare-result.schema.json",
    "CLOSEOUT-CANONICAL-CONTRACT.md",
    "CLOSEOUT-CANONICAL-CONTRACT.sha256",
    "agent-remediation-queue/*.json",
    "agent-remediation-results/*.json",
    "manual-prune/*.json",
    "manual-prune/*.bundle",
    "state.json",
    "events.jsonl",
    "trusted-approval-nonce.public.json",
    "trusted-provenance-key.public.json",
]
IMPLEMENTED_CLOSEOUT_CLI_SUBCOMMANDS = [
    "open",
    "trigger",
    "status",
    "codex-review",
    "agent-review",
    "approve",
    "validate-apply",
]
IMPLEMENTED_CLOSEOUT_STATES = [
    "opened",
    "awaiting_codex_review",
    "codex_reviewing",
    "reviewed",
    "awaiting_user_approval",
    "approved",
    "applying",
    "applied",
    "blocked",
    "failed",
    "aborted",
    "parked",
]

RISK_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "R0": {
        "label": "report-only",
        "mutates_state": False,
        "requires_explicit_approval": False,
        "default_allowed_actions": ["retain", "ask"],
        "never_allowed_reason": None,
    },
    "R1": {
        "label": "generated hygiene state",
        "mutates_state": True,
        "requires_explicit_approval": False,
        "default_allowed_actions": ["repo_hygiene_prune_old_runs", "retain"],
        "never_allowed_reason": None,
    },
    "R2": {
        "label": "git-safe reversible local cleanup",
        "mutates_state": True,
        "requires_explicit_approval": True,
        "default_allowed_actions": ["branch_archive_delete", "worktree_remove", "retain"],
        "never_allowed_reason": None,
    },
    "R3": {
        "label": "filesystem quarantine",
        "mutates_state": True,
        "requires_explicit_approval": True,
        "default_allowed_actions": ["orphan_quarantine", "retain"],
        "never_allowed_reason": None,
    },
    "R4": {
        "label": "manual-only ambiguous state",
        "mutates_state": False,
        "requires_explicit_approval": True,
        "default_allowed_actions": ["stash_promote", "retain", "ask"],
        "never_allowed_reason": "manual review required before mutation",
    },
    "R5": {
        "label": "never allowed",
        "mutates_state": False,
        "requires_explicit_approval": True,
        "default_allowed_actions": ["retain"],
        "never_allowed_reason": "operation is outside repo hygiene safety policy",
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "policy_version": POLICY_VERSION,
    "state_root": ".claude-state/repo-hygiene",
    "sensitive_roots": {
        "curated": [".claude"],
        "load_bearing": [".claude/worktrees"],
        "scratch": [".claude-state"],
    },
    "root_registry": {
        ".claude": "curated-agent-context",
        ".claude-state": "ignored-agent-scratch",
        ".claude/worktrees": "load-bearing-local-worktrees",
        "tools/agent-bridge": "agent-bridge-tooling",
        "tools/repo_hygiene": "portable-hygiene-package",
        "tools/repo-hygiene": "repo-local-hygiene-policy-and-cli",
        "docs": "tracked-documentation",
        ".github/workflows": "ci-workflows",
        "tests": "tracked-test-scaffold",
        "platform/qt/FFmpeg": "external-runtime-binary-area",
        "platform/qt/raw2mlv": "vendored-or-external-tooling",
        "src/librtprocess": "vendored-or-external-library",
        "src/mlv/liblj92": "vendored-or-external-library",
        "platform/qt/avir": "vendored-or-external-library",
        "platform/qt/maddy": "vendored-or-external-library",
        "platform/build-*": "ignored-build-output",
        "**/build-*": "ignored-build-output",
    },
    "integration_base": {
        "explicit": None,
        "fallbacks": ["origin/HEAD", "origin/master", "origin/main", "master", "main"],
        "protected_branch_patterns": ["master", "main", "develop", "release/*"],
        "remote_freshness_required_for_apply": True,
    },
    "thresholds": {
        "generated_run_retention_days": 30,
        "generated_run_keep_latest": 20,
        "worktree_stale_days": 7,
        "stash_report_days": 14,
        "orphan_size_mb": 512,
        "observation_hours": 24,
        "quarantine_retention_days": 30,
        "archive_ref_retention_days": 90,
        "dashboard_cooldown_hours": 24,
        "apply_lock_stale_hours": 1,
    },
    "allowed_roots": {
        "R1": [
            ".claude-state/repo-hygiene/runs",
            ".claude-state/repo-hygiene/quarantine",
        ],
        "R3": [".claude/worktrees"],
    },
    "tracked_ignored_allowlist": [
        "osx_installer/BuildInstaller.sh",
        "platform/mlv_blender/build.sh",
        "tools/gpu/backend/build-backend-dll.ps1",
        "tools/gpu/amaze-debayer-stage-build-run.ps1",
        "tools/gpu/build-cuda.ps1",
        "tools/gpu/glinterop/glinterop-build-run.ps1",
        "tools/gpu/oracle/build-oracle.ps1",
        "tools/gpu/pipeline-probe-build-run.ps1",
        "tools/gpu/recon-amaze-parity-build-run.ps1",
        "tools/gpu/recon-opt-build-run.ps1",
        "tools/gpu/recon-parity-build-run.ps1",
        "tools/gpu/recon-probe-build-run.ps1",
        "tools/gpu/smoke-build-run.ps1",
    ],
    "required_ignore_samples": {
        "must_be_ignored": [
            ".claude-state/probe.tmp",
            ".claude/worktrees/probe",
            ".hypothesis/probe",
            "tools/repo_hygiene/__pycache__/x.pyc",
            "monitor-probe.runtime.json",
            "platform/qt/FFmpeg/ffmpeg.exe",
        ],
        "must_not_be_ignored": [
            "tools/repo_hygiene/core.py",
            "tools/repo-hygiene/POLICY.md",
            "tools/repo-hygiene/closeout.contract.json",
            ".github/workflows/tests.yml",
            "tests/README.md",
        ],
    },
    "dirty_triage": {
        "current_work_paths": [
            "tools/agent-bridge/**",
            "tools/repo_hygiene/**",
            "tools/repo-hygiene/**",
        ],
        "generated_patterns": [
            ".claude-state/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "platform/**/build*/**",
            "**/build-*/**",
            ".hypothesis/**",
        ],
        "source_patterns": ["**/*.py", "**/*.cpp", "**/*.c", "**/*.h", "**/*.hpp", "**/*.ps1"],
        "test_patterns": ["tests/**", "tools/**/test_*.py", "**/*test*.cpp"],
        "config_patterns": ["**/*.json", "**/*.yml", "**/*.yaml", "**/*.toml", "**/*.pro", ".gitignore"],
        "doc_patterns": ["**/*.md", "docs/**"],
    },
    "portability": {
        "risk_tiers": IMPLEMENTED_RISK_TIERS,
        "candidate_kinds": IMPLEMENTED_CANDIDATE_KINDS,
        "action_ids": IMPLEMENTED_ACTION_IDS,
        "dashboard_action_ids": IMPLEMENTED_DASHBOARD_ACTION_IDS,
        "closeout_candidate_kinds": IMPLEMENTED_CLOSEOUT_CANDIDATE_KINDS,
        "closeout_action_ids": IMPLEMENTED_CLOSEOUT_ACTION_IDS,
        "closeout_publish_modes": IMPLEMENTED_CLOSEOUT_PUBLISH_MODES,
        "closeout_trigger_signal_ids": IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS,
        "closeout_contract": str(CONTRACT_PATH),
        "policy_doc": str(POLICY_DOC_PATH),
        "test_modules": ["tools/repo_hygiene/test_repo_hygiene.py"],
        "required_doc_tokens": [
            "R0",
            "R1",
            "R2",
            "R3",
            "R4",
            "R5",
            "candidate ID",
            "policy hash",
            "repo_hygiene_prune_old_runs",
            "dirty-file triage",
            "stash_promote",
            "closeout transaction",
            "Codex review",
            "trusted approval",
            "auto trigger",
            "clean_feature_branch_ready_to_publish",
            "closeout.contract.json",
        ],
    },
    "closeout": {
        "publish_modes": IMPLEMENTED_CLOSEOUT_PUBLISH_MODES,
        "default_publish_mode": "no_publish",
        "allowed_publish_remotes": ["fork"],
        "direct_push_branch_patterns": ["codex/*", "hygiene/*"],
        "trusted_approval_sources": ["codex_desktop", "dashboard_trusted_adapter", "local_interactive_cli"],
        "allowed_review_sources": ["codex_background_agent", "manual_read_only_agent"],
        "require_codex_review": True,
        "required_read_only_reviewers": 2,
        "allow_review_waiver": False,
        "nonterminal_states": [
            "awaiting_codex_review",
            "codex_reviewing",
            "reviewed",
            "awaiting_user_approval",
            "approved",
            "applying",
            "blocked",
            "parked",
        ],
        "auto_trigger": {
            "enabled": True,
            "minimum_score": 1.0,
            "signals": IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS,
        },
    },
}


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str


class HygieneError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def stable_id(kind: str, key: Any) -> str:
    return f"{kind}:{sha256_text(canonical_json(key), 16)}"


def evidence_hash(evidence: Dict[str, Any]) -> str:
    return sha256_text(canonical_json(evidence), 32)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_known_keys(override: Dict[str, Any], schema: Dict[str, Any], prefix: str = "") -> List[str]:
    failures: List[str] = []
    for key, value in override.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in schema:
            failures.append(path)
            continue
        if isinstance(value, dict) and isinstance(schema.get(key), dict):
            failures.extend(validate_known_keys(value, schema[key], path))
    return failures


def validate_repo_relative_value(value: str, label: str, *, allow_glob: bool = False) -> Optional[str]:
    raw = str(value).replace("\\", "/").strip()
    if not raw:
        return f"{label} is empty"
    if Path(raw).is_absolute():
        return f"{label} must be repo-relative: {raw}"
    parts = [part for part in raw.split("/") if part]
    if any(part == ".." for part in parts):
        return f"{label} must not contain parent traversal: {raw}"
    if not allow_glob and any(ch in raw for ch in "*?[]"):
        return f"{label} must not contain glob syntax: {raw}"
    return None


def validate_config_paths(config: Dict[str, Any]) -> List[str]:
    failures: List[str] = []
    state = str(config.get("state_root", ""))
    problem = validate_repo_relative_value(state, "state_root")
    if problem:
        failures.append(problem)
    elif not normalize_rel(state).startswith(".claude-state/"):
        failures.append("state_root must stay under .claude-state/")
    for tier, roots in config.get("allowed_roots", {}).items():
        for root in roots:
            problem = validate_repo_relative_value(str(root), f"allowed_roots.{tier}")
            if problem:
                failures.append(problem)
    portability = config.get("portability", {})
    for label, value in [
        ("portability.policy_doc", portability.get("policy_doc")),
        ("portability.closeout_contract", portability.get("closeout_contract")),
    ]:
        if value:
            problem = validate_repo_relative_value(str(value), label)
            if problem:
                failures.append(problem)
    for value in portability.get("test_modules", []):
        problem = validate_repo_relative_value(str(value), "portability.test_modules")
        if problem:
            failures.append(problem)
    return failures


def run_command(
    args: Sequence[str],
    cwd: Path,
    check: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> CommandResult:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
    )
    result = CommandResult(list(args), completed.returncode, completed.stdout, completed.stderr)
    if check and completed.returncode != 0:
        raise HygieneError(
            "command failed: %s\n%s" % (" ".join(args), completed.stderr.strip() or completed.stdout.strip())
        )
    return result


def run_git(repo_root: Path, args: Sequence[str], check: bool = False) -> CommandResult:
    return run_command(["git", *args], cwd=repo_root, check=check)


def resolve_repo_root(path: Path) -> Path:
    result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path, check=True)
    return Path(result.stdout.strip()).resolve()


def load_config(repo_root: Path) -> Dict[str, Any]:
    path = repo_root / CONFIG_PATH
    override = load_json(path, {})
    unknown_keys = validate_known_keys(override, DEFAULT_CONFIG)
    if unknown_keys:
        raise HygieneError("unknown hygiene config key(s): %s" % ", ".join(sorted(unknown_keys)))
    config = deep_merge(DEFAULT_CONFIG, override)
    path_failures = validate_config_paths(config)
    if path_failures:
        raise HygieneError("invalid hygiene config path(s): %s" % "; ".join(path_failures))
    config["policy_hash"] = sha256_text(canonical_json(config), 32)
    return config


def state_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    return (repo_root / str(config.get("state_root", ".claude-state/repo-hygiene"))).resolve()


def relpath(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def normalize_rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    value = normalize_rel(path)
    return any(fnmatch.fnmatch(value, pattern.replace("\\", "/")) for pattern in patterns)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_days(path: Path) -> Optional[float]:
    try:
        return (time.time() - path.stat().st_mtime) / 86400.0
    except OSError:
        return None


def path_inventory(path: Path, max_files: int = 2000) -> Dict[str, Any]:
    if is_reparse_point(path):
        return {
            "file_count": 0,
            "dir_count": 0,
            "bytes": 0,
            "contains_git": False,
            "source_like_sample": [],
            "sample": [],
            "truncated": False,
            "refused_reparse_point": True,
        }
    file_count = 0
    dir_count = 0
    total_bytes = 0
    source_like: List[str] = []
    contains_git = False
    sample: List[str] = []
    stopped = False
    source_suffixes = {".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".ps1", ".pro", ".json", ".md"}
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        dirs[:] = [name for name in dirs if not is_reparse_point(root_path / name)]
        if ".git" in dirs:
            contains_git = True
        dir_count += len(dirs)
        for name in files:
            file_path = root_path / name
            rel = normalize_rel(str(file_path.relative_to(path)))
            if name == ".git":
                contains_git = True
            file_count += 1
            if len(sample) < 20:
                sample.append(rel)
            if file_path.suffix.lower() in source_suffixes and len(source_like) < 20:
                source_like.append(rel)
            try:
                total_bytes += file_path.stat().st_size
            except OSError:
                pass
            if file_count >= max_files:
                stopped = True
                break
        if stopped:
            break
    return {
        "file_count": file_count,
        "dir_count": dir_count,
        "bytes": total_bytes,
        "contains_git": contains_git,
        "source_like_sample": source_like,
        "sample": sample,
        "truncated": stopped,
    }


def is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return path.is_symlink()
    try:
        return bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except AttributeError:
        return path.is_symlink()
    except OSError:
        return False


def logical_abs(path: Path) -> Path:
    if path.is_absolute():
        return path.absolute()
    return (Path.cwd() / path).absolute()


def path_under_repo_rel(repo_root: Path, path: Path, rel_root: str) -> bool:
    return real_under(path, (repo_root / rel_root).resolve())


def under_load_bearing_root(repo_root: Path, config: Dict[str, Any], path: Path) -> bool:
    roots = config.get("sensitive_roots", {}).get("load_bearing", [])
    return any(path_under_repo_rel(repo_root, path, str(root)) for root in roots)


def command_record(result: CommandResult, *, mutates: bool) -> Dict[str, Any]:
    return {
        "argv": result.args,
        "returncode": result.returncode,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
        "mutates_state": mutates,
    }


def operation_record(operation: str, *, path: Path, mutates: bool, status: str) -> Dict[str, Any]:
    return {
        "operation": operation,
        "path": str(path),
        "returncode": 0 if status == "ok" else 1,
        "stdout": "",
        "stderr": "",
        "mutates_state": mutates,
        "status": status,
    }
    try:
        return bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return path.is_symlink()


def real_under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
        if os.name == "nt":
            left = os.path.normcase(str(resolved))
            right = os.path.normcase(str(resolved_root))
            return left == right or left.startswith(right.rstrip("\\/") + os.sep)
        resolved.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def git_common_dir(repo_root: Path) -> Path:
    result = run_git(repo_root, ["rev-parse", "--git-common-dir"], check=True)
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def transient_git_state(repo_root: Path) -> Dict[str, Any]:
    common = git_common_dir(repo_root)
    markers = {
        "merge": common / "MERGE_HEAD",
        "rebase_apply": common / "rebase-apply",
        "rebase_merge": common / "rebase-merge",
        "cherry_pick": common / "CHERRY_PICK_HEAD",
        "bisect": common / "BISECT_LOG",
        "index_lock": common / "index.lock",
    }
    present = [name for name, marker in markers.items() if marker.exists()]
    return {"blocked": bool(present), "markers": present}


def resolve_integration_base(repo_root: Path, config: Dict[str, Any], trust_local_base: bool = False) -> Dict[str, Any]:
    base_config = config.get("integration_base", {})
    candidates: List[str] = []
    explicit = base_config.get("explicit")
    if explicit:
        candidates.append(str(explicit))
    else:
        candidates.extend(str(item) for item in base_config.get("fallbacks", []))

    resolved: List[Dict[str, str]] = []
    for candidate in candidates:
        result = run_git(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        if result.returncode == 0:
            resolved.append({"name": candidate, "commit": result.stdout.strip()})
    distinct = sorted({item["commit"] for item in resolved})
    if not resolved:
        return {"status": "missing", "name": None, "commit": None, "freshness": "none", "candidates": []}
    if explicit:
        chosen = resolved[0]
        ambiguous = False
    elif len(distinct) == 1:
        chosen = resolved[0]
        ambiguous = False
    else:
        chosen = resolved[0]
        ambiguous = True

    freshness = "local"
    if chosen["name"].startswith("origin/"):
        if trust_local_base:
            freshness = "trusted-local"
        else:
            freshness = "unknown"
    return {
        "status": "ambiguous" if ambiguous else "ok",
        "name": chosen["name"],
        "commit": chosen["commit"],
        "freshness": freshness,
        "candidates": resolved,
    }


def parse_worktrees(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["worktree", "list", "--porcelain"])
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    worktrees: List[Dict[str, Any]] = []
    for block in blocks:
        item: Dict[str, Any] = {"locked": False, "detached": False, "bare": False}
        for line in block:
            if line.startswith("worktree "):
                item["path"] = str(Path(line[len("worktree ") :]).resolve())
            elif line.startswith("HEAD "):
                item["head"] = line[len("HEAD ") :]
            elif line.startswith("branch "):
                item["branch"] = line[len("branch refs/heads/") :] if "refs/heads/" in line else line[len("branch ") :]
            elif line == "detached":
                item["detached"] = True
            elif line == "bare":
                item["bare"] = True
            elif line.startswith("locked"):
                item["locked"] = True
                item["lock_reason"] = line.partition(" ")[2]
        if item.get("path"):
            worktrees.append(item)
    return worktrees


def parse_status(repo_root: Path, worktree_path: Optional[Path] = None) -> Dict[str, Any]:
    cwd = worktree_path or repo_root
    result = run_command(
        # quotepath off: the v2 text format below is line-parsed, and a quoted non-ASCII path
        # would name another path (bus de09cae).
        ["git", "-c", "core.quotepath=false", "status", "--porcelain=v2", "--branch", "--untracked-files=all"],
        cwd=cwd,
    )
    branch: Dict[str, Any] = {}
    entries: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch["head"] = line[len("# branch.head ") :]
        elif line.startswith("# branch.upstream "):
            branch["upstream"] = line[len("# branch.upstream ") :]
        elif line.startswith("# branch.ab "):
            branch["ahead_behind"] = line[len("# branch.ab ") :]
        elif line.startswith("1 ") or line.startswith("2 "):
            parts = line.split(" ", 8)
            path = parts[-1] if len(parts) >= 9 else line
            entries.append({"status": parts[1] if len(parts) > 1 else "??", "path": path})
        elif line.startswith("? "):
            entries.append({"status": "?", "path": line[2:]})
        elif line.startswith("! "):
            entries.append({"status": "!", "path": line[2:]})
    return {"branch": branch, "entries": entries, "clean": not entries, "raw": result.stdout}


def local_branches(repo_root: Path, base: Dict[str, Any]) -> List[Dict[str, Any]]:
    fmt = "%(refname:short)%00%(objectname)%00%(upstream:short)%00%(committerdate:iso-strict)"
    result = run_git(repo_root, ["for-each-ref", "refs/heads", f"--format={fmt}"])
    branches: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        name, tip, upstream, date = (line.split("\x00") + ["", "", "", ""])[:4]
        if not name:
            continue
        ahead = behind = None
        if upstream:
            ab = run_git(repo_root, ["rev-list", "--left-right", "--count", f"{name}...{upstream}"])
            if ab.returncode == 0:
                left, right = ab.stdout.strip().split()[:2]
                ahead, behind = int(left), int(right)
        merged_to_base = False
        if base.get("commit"):
            merged_to_base = run_git(repo_root, ["merge-base", "--is-ancestor", tip, str(base["commit"])]).returncode == 0
        reachable_from_upstream = False
        if upstream:
            reachable_from_upstream = run_git(repo_root, ["merge-base", "--is-ancestor", tip, upstream]).returncode == 0
        branches.append(
            {
                "name": name,
                "tip": tip,
                "upstream": upstream or None,
                "committerdate": date,
                "ahead": ahead,
                "behind": behind,
                "merged_to_base": merged_to_base,
                "reachable_from_upstream": reachable_from_upstream,
            }
        )
    return branches


def stash_entries(repo_root: Path) -> List[Dict[str, Any]]:
    result = run_git(repo_root, ["stash", "list", "--format=%gd%x00%H%x00%ci%x00%s"])
    entries: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = (line.split("\x00") + ["", "", "", ""])[:4]
        name, sha, date, message = parts
        if not name:
            continue
        files = run_git(repo_root, ["-c", "core.quotepath=false", "stash", "show", "--name-only", "--format=", name])
        parents = run_git(repo_root, ["rev-list", "--parents", "-n", "1", sha])
        entries.append(
            {
                "name": name,
                "sha": sha,
                "date": date,
                "message": message,
                "files": [line.strip() for line in files.stdout.splitlines() if line.strip()],
                "parents": parents.stdout.strip().split()[1:] if parents.returncode == 0 else [],
            }
        )
    return entries


def protected_branch(name: str, config: Dict[str, Any]) -> bool:
    patterns = config.get("integration_base", {}).get("protected_branch_patterns", [])
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def make_candidate(
    *,
    kind: str,
    key: Any,
    title: str,
    risk_tier: str,
    decision: str,
    recommendation: str,
    confidence: float,
    evidence: Dict[str, Any],
    allowed_actions: Optional[List[str]] = None,
    requires_explicit_approval: Optional[bool] = None,
    preflight_requirements: Optional[List[str]] = None,
    recovery_path: Optional[str] = None,
    never_allowed_reason: Optional[str] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    risk = RISK_TAXONOMY[risk_tier]
    candidate_id = stable_id(kind, key)
    evidence = dict(evidence)
    candidate = {
        "id": candidate_id,
        "kind": kind,
        "title": title,
        "risk_tier": risk_tier,
        "risk_label": risk["label"],
        "decision": decision,
        "decision_status": "completed" if decision in {"retain", "ignore", "auto_apply", "recommend"} else "pending",
        "recommendation": recommendation,
        "confidence": round(confidence, 3),
        "evidence": evidence,
        "evidence_hash": evidence_hash(evidence),
        "allowed_actions": allowed_actions if allowed_actions is not None else list(risk["default_allowed_actions"]),
        "requires_explicit_approval": (
            bool(risk["requires_explicit_approval"])
            if requires_explicit_approval is None
            else bool(requires_explicit_approval)
        ),
        "preflight_requirements": preflight_requirements or [],
        "recovery_path": recovery_path or "No mutation is planned.",
        "never_allowed_reason": never_allowed_reason or risk.get("never_allowed_reason"),
    }
    if path:
        candidate["path"] = path
    return candidate


def dirty_recommendation(path: str, config: Dict[str, Any], recent_files: Sequence[str], branch_name: str) -> Tuple[str, float, Dict[str, Any]]:
    triage = config.get("dirty_triage", {})
    evidence: Dict[str, Any] = {
        "path": path,
        "branch": branch_name,
        "matched": [],
        "recent_commit_overlap": path in set(recent_files),
    }
    if path_matches(path, triage.get("generated_patterns", [])):
        evidence["matched"].append("generated_patterns")
        return "ignore/generated", 0.9, evidence
    score = 0.45
    disposition = "ask"
    if path_matches(path, triage.get("test_patterns", [])):
        evidence["matched"].append("test_patterns")
        disposition = "commit"
        score += 0.15
    if path_matches(path, triage.get("source_patterns", [])):
        evidence["matched"].append("source_patterns")
        disposition = "commit"
        score += 0.15
    if path_matches(path, triage.get("config_patterns", [])):
        evidence["matched"].append("config_patterns")
        disposition = "commit"
        score += 0.1
    if path_matches(path, triage.get("doc_patterns", [])):
        evidence["matched"].append("doc_patterns")
        disposition = "commit"
        score += 0.1
    if path_matches(path, triage.get("current_work_paths", [])):
        evidence["matched"].append("current_work_paths")
        score += 0.1
    if evidence["recent_commit_overlap"]:
        score += 0.1
    if disposition == "commit" and score < 0.65:
        disposition = "split"
    if not evidence["matched"]:
        disposition = "ask"
    if any(token and token.lower() in normalize_rel(path).lower() for token in branch_name.replace("-", "/").split("/")):
        evidence["matched"].append("branch_keyword_path_overlap")
        score += 0.05
    return disposition, min(score, 0.95), evidence


def recent_commit_files(repo_root: Path, limit: int = 10) -> List[str]:
    # `-z`: without it git quotes a non-ASCII path, which no prefix test matches (bus de09cae).
    result = run_git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-z", "-r", f"HEAD~{limit}..HEAD"])
    if result.returncode != 0:
        result = run_git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-z", "-r", "HEAD"])
    return [normalize_rel(part) for part in result.stdout.split("\0") if part.strip()]


def build_dirty_candidates(repo_root: Path, config: Dict[str, Any], status: Dict[str, Any]) -> List[Dict[str, Any]]:
    branch_name = str(status.get("branch", {}).get("head") or "")
    recent = recent_commit_files(repo_root)
    groups: Dict[str, Dict[str, Any]] = {}
    for entry in status.get("entries", []):
        path = normalize_rel(entry["path"])
        disposition, confidence, evidence = dirty_recommendation(path, config, recent, branch_name)
        bucket = disposition.replace("/", "-")
        group = groups.setdefault(
            bucket,
            {
                "paths": [],
                "evidence": {
                    "disposition": disposition,
                    "path_evidence": [],
                    "branch": branch_name,
                },
                "confidence": 0.0,
            },
        )
        group["paths"].append(path)
        group["evidence"]["path_evidence"].append(evidence)
        group["confidence"] = max(float(group["confidence"]), confidence)
    candidates: List[Dict[str, Any]] = []
    for bucket, group in sorted(groups.items()):
        disposition = group["evidence"]["disposition"]
        title = f"Dirty files recommended to {disposition}: {len(group['paths'])} path(s)"
        candidates.append(
            make_candidate(
                kind="dirty-group",
                key={"disposition": disposition, "paths": sorted(group["paths"])},
                title=title,
                risk_tier="R4",
                decision="retain",
                recommendation=disposition,
                confidence=float(group["confidence"]),
                evidence=group["evidence"],
                allowed_actions=["commit", "split", "stash", "ignore-generated", "ask", "retain"],
                requires_explicit_approval=True,
                preflight_requirements=["user confirms disposition", "working tree facts unchanged"],
                recovery_path="No automatic mutation in hygiene V1.5; use the disposition as a reviewed plan.",
            )
        )
    return candidates


def build_generated_report_candidates(repo_root: Path, config: Dict[str, Any], run_id: str) -> List[Dict[str, Any]]:
    root = state_root(repo_root, config) / "runs"
    threshold = int(config.get("thresholds", {}).get("generated_run_retention_days", 30))
    keep_latest = int(config.get("thresholds", {}).get("generated_run_keep_latest", 20))
    if not root.exists():
        return []
    dirs = [path for path in root.iterdir() if not is_reparse_point(path) and path.is_dir()]
    dirs.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    candidates: List[Dict[str, Any]] = []
    for index, path in enumerate(dirs):
        days = age_days(path)
        if days is None:
            continue
        if index < keep_latest or days < threshold:
            continue
        inv = path_inventory(path, max_files=500)
        evidence = {
            "relative_path": relpath(repo_root, path),
            "mtime": path.stat().st_mtime,
            "age_days": round(days, 3),
            "inventory": inv,
        }
        candidates.append(
            make_candidate(
                kind="generated-report",
                key={"path": relpath(repo_root, path)},
                title=f"Old hygiene run report: {path.name}",
                risk_tier="R1",
                decision="auto_apply",
                recommendation="repo_hygiene_prune_old_runs",
                confidence=0.98,
                evidence=evidence,
                path=str(path.resolve()),
                preflight_requirements=["path remains under R1 allowed roots", "evidence hash unchanged"],
                recovery_path="Generated run reports are reproducible by rerunning hygiene scan.",
            )
        )
    return candidates


def build_branch_candidates(
    repo_root: Path,
    config: Dict[str, Any],
    branches: Sequence[Dict[str, Any]],
    worktrees: Sequence[Dict[str, Any]],
    base: Dict[str, Any],
) -> List[Dict[str, Any]]:
    worktree_branches = {wt.get("branch") for wt in worktrees if wt.get("branch")}
    candidates: List[Dict[str, Any]] = []
    base_ok = base.get("status") == "ok"
    for branch in branches:
        name = branch["name"]
        evidence = {"branch": branch, "integration_base": base, "has_linked_worktree": name in worktree_branches}
        if protected_branch(name, config):
            candidates.append(
                make_candidate(
                    kind="branch",
                    key={"branch": name},
                    title=f"Protected branch retained: {name}",
                    risk_tier="R5",
                    decision="retain",
                    recommendation="retain",
                    confidence=1.0,
                    evidence=evidence,
                    allowed_actions=["retain"],
                    never_allowed_reason="protected branch pattern",
                )
            )
            continue
        eligible = (
            base_ok
            and name not in worktree_branches
            and (branch.get("merged_to_base") or branch.get("reachable_from_upstream"))
            and not branch.get("ahead")
        )
        if eligible:
            candidates.append(
                make_candidate(
                    kind="branch",
                    key={"branch": name},
                    title=f"Merged local branch candidate: {name}",
                    risk_tier="R2",
                    decision="recommend",
                    recommendation="branch_archive_delete",
                    confidence=0.86,
                    evidence=evidence,
                    preflight_requirements=[
                        "integration base still resolves to same commit",
                        "branch tip unchanged",
                        "branch still has no linked worktree",
                        "archive ref created before deletion",
                    ],
                    recovery_path="Restore with: git branch <name> <archive-ref>.",
                )
            )
        else:
            candidates.append(
                make_candidate(
                    kind="branch",
                    key={"branch": name},
                    title=f"Branch retained/manual: {name}",
                    risk_tier="R4",
                    decision="retain",
                    recommendation="ask",
                    confidence=0.7,
                    evidence=evidence,
                    allowed_actions=["retain", "ask"],
                    never_allowed_reason=None if base_ok else "integration base ambiguous or missing",
                )
            )
    return candidates


def build_worktree_candidates(
    repo_root: Path,
    config: Dict[str, Any],
    worktrees: Sequence[Dict[str, Any]],
    base: Dict[str, Any],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    current_cwd = str(Path.cwd().resolve())
    for wt in worktrees:
        path = Path(str(wt["path"]))
        if path.resolve() == repo_root.resolve():
            continue
        status = parse_status(repo_root, path)
        evidence = {
            "worktree": wt,
            "relative_path": relpath(repo_root, path),
            "status_clean": status["clean"],
            "status_entries": status["entries"][:20],
            "integration_base": base,
            "current_cwd": current_cwd,
        }
        load_bearing = under_load_bearing_root(repo_root, config, path)
        eligible = (
            status["clean"]
            and not wt.get("locked")
            and not load_bearing
            and os.path.normcase(str(path.resolve())) != os.path.normcase(current_cwd)
            and base.get("status") == "ok"
        )
        candidates.append(
            make_candidate(
                kind="worktree",
                key={"path": str(path.resolve())},
                title=("Clean registered worktree" if eligible else "Registered worktree retained") + f": {path.name}",
                risk_tier="R2" if eligible else "R4",
                decision="recommend" if eligible else "retain",
                recommendation="worktree_remove" if eligible else "retain",
                confidence=0.82 if eligible else 0.72,
                evidence=evidence,
                path=str(path.resolve()),
                allowed_actions=["worktree_remove", "retain"] if eligible else ["retain", "ask"],
                preflight_requirements=[
                    "worktree still registered",
                    "worktree status remains clean",
                    "worktree remains unlocked",
                    "candidate is not current cwd",
                    "git worktree remove succeeds without --force",
                ],
                recovery_path="Re-add with git worktree add <path> <branch-or-commit> if commit remains reachable.",
                never_allowed_reason=(
                    "load-bearing worktree root"
                    if load_bearing
                    else (None if eligible else "dirty, locked, current, or ambiguous worktree state")
                ),
            )
        )
    return candidates


def build_orphan_candidates(repo_root: Path, config: Dict[str, Any], worktrees: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root = (repo_root / ".claude" / "worktrees").resolve()
    if not root.exists():
        return []
    registered = {os.path.normcase(str(Path(str(wt["path"])).resolve())) for wt in worktrees}
    threshold_bytes = int(config.get("thresholds", {}).get("orphan_size_mb", 512)) * 1024 * 1024
    candidates: List[Dict[str, Any]] = []
    children: List[Path] = []
    for path in root.iterdir():
        if is_reparse_point(path):
            children.append(path)
        elif path.is_dir():
            children.append(path)
    for child in sorted(children):
        child_resolved = logical_abs(child)
        reparse = is_reparse_point(child)
        if os.path.normcase(str(child_resolved)) in registered:
            continue
        if reparse:
            inv = {
                "file_count": 0,
                "dir_count": 0,
                "bytes": 0,
                "contains_git": False,
                "source_like_sample": [],
                "sample": [],
                "truncated": False,
                "refused_reparse_point": True,
            }
        else:
            inv = path_inventory(child, max_files=2000)
        cache_only = inv["file_count"] == 0 or all(part.startswith(".hypothesis") for part in inv["sample"])
        big = int(inv["bytes"]) >= threshold_bytes
        evidence = {
            "relative_path": relpath(repo_root, child),
            "inventory": inv,
            "registered_worktree": False,
            "cache_only": cache_only,
            "size_threshold_bytes": threshold_bytes,
            "is_reparse_point": reparse,
        }
        eligible = (cache_only or big) and not inv["contains_git"] and not inv["source_like_sample"] and not reparse
        candidates.append(
            make_candidate(
                kind="orphan-dir",
                key={"path": str(child_resolved)},
                title=("Orphan directory quarantine candidate" if eligible else "Orphan directory manual review") + f": {child.name}",
                risk_tier="R3" if eligible else "R4",
                decision="recommend" if eligible else "retain",
                recommendation="orphan_quarantine" if eligible else "ask",
                confidence=0.78 if eligible else 0.55,
                evidence=evidence,
                path=str(child_resolved),
                allowed_actions=["orphan_quarantine", "retain"] if eligible else ["retain", "ask"],
                preflight_requirements=[
                    "path remains a direct child of .claude/worktrees",
                    "path is still unregistered",
                    "path inventory hash unchanged",
                    "path is not a symlink, junction, or reparse point",
                    "atomic rename to quarantine succeeds",
                ],
                recovery_path="Restore by moving the quarantined directory back to its original path before purge.",
                never_allowed_reason=None if eligible else "contains source-like, Git, reparse, or ambiguous content",
            )
        )
    return candidates


def build_stash_candidates(repo_root: Path, config: Dict[str, Any], stashes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for stash in stashes:
        evidence = {"stash": stash, "file_count": len(stash.get("files") or [])}
        code_bearing = any(path_matches(path, config.get("dirty_triage", {}).get("source_patterns", [])) for path in stash.get("files") or [])
        candidates.append(
            make_candidate(
                kind="stash",
                key={"sha": stash["sha"]},
                title=f"Stash retained: {stash['name']}",
                risk_tier="R4",
                decision="retain",
                recommendation="stash_promote" if code_bearing else "ask",
                confidence=0.8 if code_bearing else 0.6,
                evidence=evidence,
                allowed_actions=["stash_promote", "retain", "ask"],
                requires_explicit_approval=True,
                preflight_requirements=["stash sha still exists", "promotion branch/worktree does not collide"],
                recovery_path="Promotion creates a hygiene/stash/<sha> branch and optional recovery worktree without dropping the stash.",
                never_allowed_reason="stash drop is not allowed by V3 policy",
            )
        )
    return candidates


def observation_path(repo_root: Path, config: Dict[str, Any]) -> Path:
    return state_root(repo_root, config) / "observations.json"


def update_observations(repo_root: Path, config: Dict[str, Any], candidates: List[Dict[str, Any]]) -> None:
    path = observation_path(repo_root, config)
    data = load_json(path, {"schema_version": SCHEMA_VERSION, "candidates": {}})
    now = utc_now()
    for candidate in candidates:
        current = data["candidates"].setdefault(candidate["id"], {})
        if current.get("evidence_hash") != candidate["evidence_hash"]:
            current.clear()
            current["first_seen_at"] = now
        else:
            current.setdefault("first_seen_at", now)
        current["last_seen_at"] = now
        current["evidence_hash"] = candidate["evidence_hash"]
        current["kind"] = candidate["kind"]
        candidate["observation"] = dict(current)
    write_json(path, data)


def attach_observations(repo_root: Path, config: Dict[str, Any], candidates: List[Dict[str, Any]]) -> None:
    data = load_json(observation_path(repo_root, config), {"candidates": {}})
    required = float(config.get("thresholds", {}).get("observation_hours", 24))
    now_dt = datetime.now(timezone.utc)
    for candidate in candidates:
        obs = dict(data.get("candidates", {}).get(candidate["id"], {}))
        first = parse_iso(obs.get("first_seen_at"))
        ready = False
        if first and obs.get("evidence_hash") == candidate["evidence_hash"]:
            ready = (now_dt - first).total_seconds() >= required * 3600
        obs["two_observation_ready"] = ready
        obs["required_hours"] = required
        candidate["observation"] = obs


def build_facts(repo_root: Path, active_root: Path, config: Dict[str, Any], trust_local_base: bool = False) -> Dict[str, Any]:
    base = resolve_integration_base(repo_root, config, trust_local_base=trust_local_base)
    worktrees = parse_worktrees(repo_root)
    status = parse_status(repo_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_hash": config["policy_hash"],
        "repo_root": str(repo_root),
        "active_root": str(active_root),
        "timestamp": utc_now(),
        "git_version": run_git(repo_root, ["--version"]).stdout.strip(),
        "capability_probe": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "os_name": os.name,
            "path_case_probe": "case-insensitive-likely" if os.name == "nt" else "case-sensitive-likely",
            "symlink_or_reparse_supported": True,
            "atomic_replace_used": True,
            "shell_required": False,
        },
        "root_registry": config.get("root_registry", {}),
        "head": run_git(repo_root, ["rev-parse", "HEAD"]).stdout.strip(),
        "transient_git_state": transient_git_state(repo_root),
        "integration_base": base,
        "status": status,
        "worktrees": worktrees,
        "branches": local_branches(repo_root, base),
        "stashes": stash_entries(repo_root),
        "worktree_prune_dry_run": run_git(repo_root, ["worktree", "prune", "--dry-run", "--verbose"]).stdout,
    }


def build_plan(repo_root: Path, active_root: Path, config: Dict[str, Any], trust_local_base: bool = False) -> Dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    facts = build_facts(repo_root, active_root, config, trust_local_base=trust_local_base)
    candidates: List[Dict[str, Any]] = []
    candidates.extend(build_generated_report_candidates(repo_root, config, run_id))
    candidates.extend(build_dirty_candidates(repo_root, config, facts["status"]))
    candidates.extend(build_branch_candidates(repo_root, config, facts["branches"], facts["worktrees"], facts["integration_base"]))
    candidates.extend(build_worktree_candidates(repo_root, config, facts["worktrees"], facts["integration_base"]))
    candidates.extend(build_orphan_candidates(repo_root, config, facts["worktrees"]))
    candidates.extend(build_stash_candidates(repo_root, config, facts["stashes"]))
    attach_observations(repo_root, config, candidates)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_hash": config["policy_hash"],
        "run_id": run_id,
        "repo_root": str(repo_root),
        "active_root": str(active_root),
        "generated_at": utc_now(),
        "risk_taxonomy": RISK_TAXONOMY,
        "facts_ref": "facts.json",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "dashboard_actions": [
            {
                "id": action_id,
                "safe_to_run": action_id in IMPLEMENTED_DASHBOARD_ACTION_IDS,
                "mutates_state": True,
                "symbolic_only": True,
            }
            for action_id in IMPLEMENTED_DASHBOARD_ACTION_IDS
        ],
    }
    return {"facts": facts, "plan": plan}


def summarize(plan: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> str:
    lines: List[str] = []
    if result:
        lines.append(f"Outcome: {result.get('status')} ({result.get('message')})")
    else:
        lines.append(f"Outcome: dry-run only; {plan.get('candidate_count', 0)} candidate(s) found")
    sections = [
        ("Safe to apply", lambda c: c["risk_tier"] == "R1" and c["decision"] == "auto_apply"),
        ("Recommended review", lambda c: c["decision"] == "recommend"),
        ("Retained intentionally", lambda c: c["decision"] == "retain" and c["risk_tier"] != "R5"),
        ("Blocked", lambda c: c.get("decision") == "blocked" or c["risk_tier"] == "R5"),
    ]
    for title, predicate in sections:
        items = [c for c in plan.get("candidates", []) if predicate(c)]
        lines.append("")
        lines.append(f"## {title}")
        if not items:
            lines.append("- none")
            continue
        for candidate in items[:50]:
            lines.append(
                "- {id} [{risk}] {rec} ({conf:.2f}): {title}".format(
                    id=candidate["id"],
                    risk=candidate["risk_tier"],
                    rec=candidate["recommendation"],
                    conf=float(candidate["confidence"]),
                    title=candidate["title"],
                )
            )
            lines.append(f"  evidence={candidate['evidence_hash']} approval={candidate['requires_explicit_approval']}")
        if len(items) > 50:
            lines.append(f"- ... {len(items) - 50} more in JSON")
    return "\n".join(lines) + "\n"


def run_scan(
    repo_root_arg: Path,
    *,
    write_artifacts_flag: bool = True,
    trust_local_base: bool = False,
    update_observation_state: bool = True,
) -> Dict[str, Any]:
    active_root = repo_root_arg.resolve()
    repo_root = resolve_repo_root(active_root)
    config = load_config(repo_root)
    built = build_plan(repo_root, active_root, config, trust_local_base=trust_local_base)
    facts = built["facts"]
    plan = built["plan"]
    if update_observation_state:
        update_observations(repo_root, config, plan["candidates"])
    summary = summarize(plan)
    run_dir: Optional[Path] = None
    if write_artifacts_flag:
        run_dir = state_root(repo_root, config) / "runs" / plan["run_id"]
        write_json(run_dir / "facts.json", facts)
        write_json(run_dir / "plan.json", plan)
        write_json(
            run_dir / "result.json",
            {
                "schema_version": SCHEMA_VERSION,
                "policy_version": POLICY_VERSION,
                "policy_hash": config["policy_hash"],
                "run_id": plan["run_id"],
                "status": "dry_run",
                "message": "scan completed without mutation",
                "commands_invoked": [],
                "outcomes": [],
            },
        )
        (run_dir / "summary.md").write_text(summary, encoding="utf-8")
    return {"facts": facts, "plan": plan, "summary": summary, "run_dir": str(run_dir) if run_dir else None}


def remove_tree(path: Path) -> None:
    shutil.rmtree(path)


@contextlib.contextmanager
def apply_mutex(repo_root: Path, config: Dict[str, Any]) -> Iterator[Path]:
    lock_path = state_root(repo_root, config) / "apply.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stale_hours = float(config.get("thresholds", {}).get("apply_lock_stale_hours", 1))
    now = time.time()
    if lock_path.exists():
        age_hours = (now - lock_path.stat().st_mtime) / 3600.0
        if age_hours <= stale_hours:
            raise HygieneError(f"repo hygiene apply lock exists: {lock_path}")
        lock_path.unlink()
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "cwd": str(Path.cwd()),
                    "started_at": utc_now(),
                    "policy_version": POLICY_VERSION,
                },
                handle,
                sort_keys=True,
            )
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def find_candidate(plan: Dict[str, Any], candidate_id: str) -> Dict[str, Any]:
    for candidate in plan.get("candidates", []):
        if candidate.get("id") == candidate_id:
            return candidate
    raise HygieneError(f"candidate not found: {candidate_id}")


def require_observation(candidate: Dict[str, Any], manual_override: bool) -> None:
    if manual_override:
        return
    if candidate["risk_tier"] in {"R2", "R3"} and not candidate.get("observation", {}).get("two_observation_ready"):
        raise HygieneError("candidate requires two matching observations or --manual-override")


def assert_no_transient_state(facts: Dict[str, Any]) -> None:
    transient = facts.get("transient_git_state", {})
    if transient.get("blocked"):
        raise HygieneError("git transient state blocks mutation: %s" % ",".join(transient.get("markers", [])))


def ensure_allowed_path(repo_root: Path, config: Dict[str, Any], path: Path, tier: str) -> None:
    allowed = config.get("allowed_roots", {}).get(tier, [])
    if not allowed:
        raise HygieneError(f"no allowed roots for {tier}")
    if is_reparse_point(path):
        raise HygieneError(f"refusing reparse/symlink path: {path}")
    if not any(real_under(path, (repo_root / item).resolve()) for item in allowed):
        raise HygieneError(f"path is outside allowed roots for {tier}: {path}")


def archive_ref_for(run_id: str, tip: str) -> str:
    return f"refs/archive/hygiene/branches/{run_id}/{tip[:12]}"


def apply_generated_prune(repo_root: Path, config: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(candidate["path"]).resolve()
    ensure_allowed_path(repo_root, config, path, "R1")
    if not path.exists():
        return {
            "changed": False,
            "message": "path already absent",
            "recovery": "rerun scan",
            "commands_invoked": [operation_record("remove_tree", path=path, mutates=True, status="already_absent")],
        }
    remove_tree(path)
    return {
        "changed": True,
        "message": f"removed generated report {path.name}",
        "recovery": "rerun scan",
        "commands_invoked": [operation_record("remove_tree", path=path, mutates=True, status="ok")],
    }


def apply_branch_delete(repo_root: Path, plan: Dict[str, Any], candidate: Dict[str, Any], trust_local_base: bool) -> Dict[str, Any]:
    evidence = candidate["evidence"]["branch"]
    name = evidence["name"]
    tip = evidence["tip"]
    base = plan.get("candidates", [])[0:0]  # keeps lint quiet for explicit plan use below
    del base
    if not trust_local_base and candidate["evidence"]["integration_base"].get("freshness") == "unknown":
        raise HygieneError("remote base freshness is unknown; pass --trust-local-base only for explicit manual apply")
    current = run_git(repo_root, ["rev-parse", "--verify", f"{name}^{{commit}}"], check=True).stdout.strip()
    if current != tip:
        raise HygieneError("branch tip changed before apply")
    ref = archive_ref_for(str(plan["run_id"]), tip)
    commands: List[Dict[str, Any]] = []
    update = run_git(repo_root, ["update-ref", ref, tip])
    commands.append(command_record(update, mutates=True))
    if update.returncode != 0:
        raise HygieneError(update.stderr.strip() or update.stdout.strip())
    deleted = run_git(repo_root, ["branch", "-d", name])
    commands.append(command_record(deleted, mutates=True))
    if deleted.returncode != 0:
        raise HygieneError(deleted.stderr.strip() or deleted.stdout.strip())
    return {
        "changed": True,
        "message": f"archived {name} at {ref} and deleted local branch",
        "archive_ref": ref,
        "recovery": f"git branch {name} {ref}",
        "commands_invoked": commands,
    }


def apply_worktree_remove(repo_root: Path, config: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(candidate["path"]).resolve()
    if under_load_bearing_root(repo_root, config, path):
        raise HygieneError("load-bearing worktree roots cannot be removed by repo hygiene")
    status = parse_status(repo_root, path)
    if not status["clean"]:
        raise HygieneError("worktree became dirty before apply")
    result = run_git(repo_root, ["worktree", "remove", str(path)])
    commands = [command_record(result, mutates=True)]
    if result.returncode != 0:
        raise HygieneError(result.stderr.strip() or result.stdout.strip())
    return {
        "changed": True,
        "message": f"removed registered worktree {path}",
        "recovery": "git worktree add <path> <branch-or-commit>",
        "commands_invoked": commands,
    }


def apply_orphan_quarantine(repo_root: Path, config: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(candidate["path"]).resolve()
    ensure_allowed_path(repo_root, config, path, "R3")
    quarantine_root = state_root(repo_root, config) / "quarantine" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_root.mkdir(parents=True, exist_ok=True)
    target = quarantine_root / path.name
    if path.anchor.lower() != target.anchor.lower():
        raise HygieneError("cross-volume quarantine move is manual-only")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate["id"],
        "original_path": str(path),
        "target_path": str(target),
        "evidence_hash": candidate["evidence_hash"],
        "inventory": candidate["evidence"].get("inventory"),
        "created_at": utc_now(),
    }
    path.rename(target)
    write_json(quarantine_root / "manifest.json", manifest)
    return {
        "changed": True,
        "message": f"quarantined orphan directory {path.name}",
        "quarantine_path": str(target),
        "recovery": f"Move {target} back to {path} before purge.",
        "commands_invoked": [
            operation_record("rename_to_quarantine", path=path, mutates=True, status="ok"),
            operation_record("write_quarantine_manifest", path=quarantine_root / "manifest.json", mutates=True, status="ok"),
        ],
    }


def apply_stash_promote(repo_root: Path, config: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    stash = candidate["evidence"]["stash"]
    sha = stash["sha"]
    verify = run_git(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"])
    if verify.returncode != 0:
        raise HygieneError("stash commit no longer exists")
    branch = f"hygiene/stash/{sha[:12]}"
    ref = f"refs/heads/{branch}"
    existing = run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    commands: List[Dict[str, Any]] = [command_record(verify, mutates=False), command_record(existing, mutates=False)]
    if existing.returncode != 0:
        update = run_git(repo_root, ["update-ref", ref, sha])
        commands.append(command_record(update, mutates=True))
        if update.returncode != 0:
            raise HygieneError(update.stderr.strip() or update.stdout.strip())
    wt_root = state_root(repo_root, config) / "stash-worktrees"
    wt_path = wt_root / sha[:12]
    if not wt_path.exists():
        wt_root.mkdir(parents=True, exist_ok=True)
        added = run_git(repo_root, ["worktree", "add", str(wt_path), branch])
        commands.append(command_record(added, mutates=True))
        if added.returncode != 0:
            raise HygieneError(added.stderr.strip() or added.stdout.strip())
    return {
        "changed": True,
        "message": f"promoted stash {stash['name']} to {branch}",
        "branch": branch,
        "worktree": str(wt_path),
        "recovery": f"Inspect {wt_path}; original stash was not dropped.",
        "commands_invoked": commands,
    }


def run_apply(
    repo_root_arg: Path,
    *,
    candidate_id: str,
    action_id: str,
    expected_evidence_hash: Optional[str] = None,
    manual_override: bool = False,
    trust_local_base: bool = False,
) -> Dict[str, Any]:
    active_root = repo_root_arg.resolve()
    repo_root = resolve_repo_root(active_root)
    config = load_config(repo_root)
    with apply_mutex(repo_root, config):
        built = build_plan(repo_root, active_root, config, trust_local_base=trust_local_base)
        facts = built["facts"]
        plan = built["plan"]
        assert_no_transient_state(facts)
        candidate = find_candidate(plan, candidate_id)
        if expected_evidence_hash and candidate["evidence_hash"] != expected_evidence_hash:
            raise HygieneError("candidate evidence hash changed before apply")
        if action_id not in candidate.get("allowed_actions", []):
            raise HygieneError(f"action {action_id} is not allowed for candidate {candidate_id}")
        mutating_action = action_id not in {"retain", "ask", "commit", "split", "stash", "ignore-generated"}
        if mutating_action and not expected_evidence_hash:
            raise HygieneError("--expected-evidence-hash is required for mutating hygiene apply actions")
        require_observation(candidate, manual_override)
        if candidate["kind"] == "generated-report" and action_id == "repo_hygiene_prune_old_runs":
            outcome = apply_generated_prune(repo_root, config, candidate)
        elif candidate["kind"] == "branch" and action_id == "branch_archive_delete":
            outcome = apply_branch_delete(repo_root, plan, candidate, trust_local_base=trust_local_base)
        elif candidate["kind"] == "worktree" and action_id == "worktree_remove":
            outcome = apply_worktree_remove(repo_root, config, candidate)
        elif candidate["kind"] == "orphan-dir" and action_id == "orphan_quarantine":
            outcome = apply_orphan_quarantine(repo_root, config, candidate)
        elif candidate["kind"] == "stash" and action_id == "stash_promote":
            outcome = apply_stash_promote(repo_root, config, candidate)
        elif action_id == "retain":
            outcome = {"changed": False, "message": "candidate intentionally retained", "recovery": "none"}
        else:
            raise HygieneError(f"unsupported action/candidate combination: {action_id}/{candidate['kind']}")
        result = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "policy_hash": config["policy_hash"],
            "run_id": plan["run_id"],
            "status": "applied" if outcome.get("changed") else "no_change",
            "message": outcome["message"],
            "candidate_id": candidate_id,
            "action_id": action_id,
            "preflight_results": {
                "transient_git_state": facts["transient_git_state"],
                "candidate_evidence_hash": candidate["evidence_hash"],
                "expected_evidence_hash": expected_evidence_hash,
                "manual_override": manual_override,
            },
            "commands_invoked": outcome.pop("commands_invoked", []),
            "outcomes": [outcome],
            "recovery_hints": [outcome.get("recovery")],
        }
        run_dir = state_root(repo_root, config) / "runs" / plan["run_id"]
        write_json(run_dir / "facts.json", facts)
        write_json(run_dir / "plan.json", plan)
        write_json(run_dir / "result.json", result)
        (run_dir / "summary.md").write_text(summarize(plan, result), encoding="utf-8")
        return {"result": result, "run_dir": str(run_dir)}


def verify_policy(repo_root_arg: Path) -> Dict[str, Any]:
    repo_root = resolve_repo_root(repo_root_arg.resolve())
    config = load_config(repo_root)
    portability = config.get("portability", {})
    failures: List[str] = []

    def require_set(name: str, expected: Sequence[str], actual: Sequence[str]) -> None:
        if sorted(expected) != sorted(actual):
            failures.append(f"{name} mismatch: config={sorted(expected)} code={sorted(actual)}")

    require_set("risk_tiers", portability.get("risk_tiers", []), IMPLEMENTED_RISK_TIERS)
    require_set("candidate_kinds", portability.get("candidate_kinds", []), IMPLEMENTED_CANDIDATE_KINDS)
    require_set("action_ids", portability.get("action_ids", []), IMPLEMENTED_ACTION_IDS)
    require_set("dashboard_action_ids", portability.get("dashboard_action_ids", []), IMPLEMENTED_DASHBOARD_ACTION_IDS)
    require_set(
        "closeout_candidate_kinds",
        portability.get("closeout_candidate_kinds", []),
        IMPLEMENTED_CLOSEOUT_CANDIDATE_KINDS,
    )
    require_set("closeout_action_ids", portability.get("closeout_action_ids", []), IMPLEMENTED_CLOSEOUT_ACTION_IDS)
    require_set(
        "closeout_publish_modes",
        portability.get("closeout_publish_modes", []),
        IMPLEMENTED_CLOSEOUT_PUBLISH_MODES,
    )
    require_set("closeout.publish_modes", config.get("closeout", {}).get("publish_modes", []), IMPLEMENTED_CLOSEOUT_PUBLISH_MODES)
    require_set(
        "closeout_trigger_signal_ids",
        portability.get("closeout_trigger_signal_ids", []),
        IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS,
    )
    require_set(
        "closeout.auto_trigger.signals",
        config.get("closeout", {}).get("auto_trigger", {}).get("signals", []),
        IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS,
    )
    contract_path = repo_root / portability.get("closeout_contract", str(CONTRACT_PATH))
    contract = load_json(contract_path, None)
    if not isinstance(contract, dict):
        failures.append(f"missing closeout contract: {contract_path}")
        contract = {}
    require_set("contract.candidate_kinds", contract.get("candidate_kinds", []), IMPLEMENTED_CLOSEOUT_CANDIDATE_KINDS)
    require_set("contract.action_ids", contract.get("action_ids", []), IMPLEMENTED_CLOSEOUT_ACTION_IDS)
    require_set("contract.artifact_names", contract.get("artifact_names", []), IMPLEMENTED_CLOSEOUT_ARTIFACT_NAMES)
    require_set("contract.cli_subcommands", contract.get("cli_subcommands", []), IMPLEMENTED_CLOSEOUT_CLI_SUBCOMMANDS)
    require_set("contract.states", contract.get("states", []), IMPLEMENTED_CLOSEOUT_STATES)
    require_set("contract.publish_modes", contract.get("publish_modes", []), IMPLEMENTED_CLOSEOUT_PUBLISH_MODES)
    require_set("contract.trigger_signal_ids", contract.get("trigger_signal_ids", []), IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS)
    require_set(
        "contract.review_sources",
        contract.get("review_sources", []),
        config.get("closeout", {}).get("allowed_review_sources", []),
    )
    require_set(
        "contract.approval_sources",
        contract.get("approval_sources", []),
        config.get("closeout", {}).get("trusted_approval_sources", []),
    )
    if contract.get("required_read_only_reviewers") != config.get("closeout", {}).get("required_read_only_reviewers"):
        failures.append("contract.required_read_only_reviewers does not match config")
    if contract.get("allow_review_waiver") != config.get("closeout", {}).get("allow_review_waiver"):
        failures.append("contract.allow_review_waiver does not match config")
    if contract.get("requires_signed_provenance") is not True:
        failures.append("contract.requires_signed_provenance must be true")
    if contract.get("role_specific_provenance_keys") is not True:
        failures.append("contract.role_specific_provenance_keys must be true")
    if contract.get("cli_secret_transport") != "environment":
        failures.append("contract.cli_secret_transport must be environment")
    if "validation is not mutation" not in str(contract.get("executor_boundary", "")):
        failures.append("contract.executor_boundary must spell out validation is not mutation")

    doc = repo_root / portability.get("policy_doc", str(POLICY_DOC_PATH))
    if not doc.exists():
        failures.append(f"missing policy doc: {doc}")
        doc_text = ""
    else:
        doc_text = doc.read_text(encoding="utf-8")
    for token in portability.get("required_doc_tokens", []):
        if token not in doc_text:
            failures.append(f"policy doc missing token: {token}")
    root_registry = config.get("root_registry", {})
    for required_root in [".claude", ".claude-state", ".claude/worktrees", "tools/repo_hygiene", "tools/repo-hygiene"]:
        if required_root not in root_registry:
            failures.append(f"root registry missing {required_root}")
    tracked_ignored = run_git(repo_root, ["ls-files", "-z", "-ci", "--exclude-standard"])
    if tracked_ignored.returncode == 0:
        allowlist = {normalize_rel(item) for item in config.get("tracked_ignored_allowlist", [])}
        for line in tracked_ignored.stdout.split("\0"):
            path = normalize_rel(line)
            if path and path not in allowlist:
                failures.append(f"tracked ignored file is not allowlisted: {path}")
    samples = config.get("required_ignore_samples", {})
    for sample in samples.get("must_be_ignored", []):
        path = normalize_rel(sample)
        check = run_git(repo_root, ["check-ignore", "--no-index", "--quiet", path])
        if check.returncode != 0:
            failures.append(f"required ignored sample is not ignored: {path}")
    for sample in samples.get("must_not_be_ignored", []):
        path = normalize_rel(sample)
        check = run_git(repo_root, ["check-ignore", "--no-index", "--quiet", path])
        if check.returncode == 0:
            failures.append(f"tracked source/doc/test sample is unexpectedly ignored: {path}")
    for test_module in portability.get("test_modules", []):
        test_path = repo_root / test_module
        if not test_path.exists():
            failures.append(f"missing test module: {test_module}")
        else:
            test_text = test_path.read_text(encoding="utf-8")
            for token in IMPLEMENTED_CANDIDATE_KINDS + IMPLEMENTED_DASHBOARD_ACTION_IDS:
                if token not in test_text:
                    failures.append(f"test module {test_module} missing token: {token}")
            for token in (
                IMPLEMENTED_CLOSEOUT_CANDIDATE_KINDS
                + IMPLEMENTED_CLOSEOUT_ACTION_IDS
                + IMPLEMENTED_CLOSEOUT_PUBLISH_MODES
                + IMPLEMENTED_CLOSEOUT_TRIGGER_SIGNAL_IDS
            ):
                if token not in test_text:
                    failures.append(f"test module {test_module} missing token: {token}")
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_hash": config["policy_hash"],
        "ok": not failures,
        "failures": failures,
    }
