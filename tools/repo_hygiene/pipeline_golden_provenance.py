"""Validate the pipeline golden provenance and cross-golden contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .repository_identity import alias_urls, is_repository_alias


DEFAULT_MANIFEST = Path("tests/fixtures/golden/pipeline_hashes.provenance.json")
EXPECTED_FULL16_KEYS = {
    "tiny_dual_iso.full16.frame0",
    "tiny_dual_iso.full16.frame1",
}
REVIEW_STATUSES = {"ratified", "unratified"}
PHASE3_REVIEW_STATUSES = {"unratified", "ratified", "git_integrated_external_review_asserted"}
PAIR_STATUSES = {"aligned", "known_mismatch"}
EXTERNAL_ASSERTION = "externally_asserted_unvalidated"
HASH_MODES = {"raw_sha256", "lf_normalized_sha256"}
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_RE = re.compile(r"^[0-9]+$")
PSNR_RE = re.compile(r"^[0-9]+\.[0-9]{4}$")
VERIFIED_APPROVAL_CLAIMS = {
    "tracked_whole_artifact_approval": {
        "schema_version": 1,
        "kind": "tracked_whole_artifact_approval",
        "verdict": "APPROVE",
        "artifact_path": "tests/fixtures/golden/pipeline_hashes.json",
        "artifact_sha256": "89ce4ad45e378f38db8bab1efe3660056dc14c16bd71d8e3ec6da08fbac01f4a",
        "reviewed_range": (
            "e87b5133397240d5ebf3307625e8379be81e48f4.."
            "08f38ea73887e9ae3b3e84673f03d82267473fab"
        ),
        "reviewer": "layibabalola",
        "reviewer_role": "repository_owner",
        "reviewed_head": "08f38ea73887e9ae3b3e84673f03d82267473fab",
        "source": {
            "kind": "github_issue_comment",
            "repository": "layibabalola/MLV-App",
            "pull_request": 6,
            "comment_id": 5347847988,
            "url": "https://github.com/layibabalola/MLV-App/pull/6#issuecomment-5347847988",
            "created_at": "2026-08-19T20:51:23Z",
            "author_association": "OWNER",
            "body_sha256": "adbba554147e3ac4afb7328a242d379e890913716df687b99b7cab4a578d4726",
        },
        "scope": {
            "key_count": 15,
            "provider_authority": False,
            "automatic_launch_gate": "CLOSED",
        },
    },
}


class ProvenanceValidationError(ValueError):
    """Raised when the provenance contract is incomplete or inconsistent."""


class GitWitness(Protocol):
    """Read-only Git evidence surface, injectable for deterministic tests."""

    def commit_exists(self, commit: str) -> bool: ...
    def blob(self, commit: str, relative_path: str) -> bytes: ...
    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...
    def parents(self, commit: str) -> list[str]: ...
    def paths_equal(self, left: str, right: str, paths: list[str]) -> bool: ...
    def last_change(self, commit: str, relative_path: str) -> str: ...


class SubprocessGitWitness:
    """Read repository history without mutating refs, index, or worktree."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def _run(self, arguments: list[str], allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if result.returncode not in allowed:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ProvenanceValidationError(f"Git evidence command failed ({' '.join(arguments)}): {detail}")
        return result

    def commit_exists(self, commit: str) -> bool:
        return self._run(["cat-file", "-e", f"{commit}^{{commit}}"], (0, 1, 128)).returncode == 0

    def blob(self, commit: str, relative_path: str) -> bytes:
        return self._run(["show", f"{commit}:{relative_path}"]).stdout

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._run(["merge-base", "--is-ancestor", ancestor, descendant], (0, 1)).returncode == 0

    def parents(self, commit: str) -> list[str]:
        output = self._run(["show", "-s", "--format=%P", commit]).stdout.decode("ascii").strip()
        return output.split() if output else []

    def paths_equal(self, left: str, right: str, paths: list[str]) -> bool:
        return self._run(["diff", "--quiet", left, right, "--", *paths], (0, 1)).returncode == 0

    def last_change(self, commit: str, relative_path: str) -> str:
        output = self._run(["log", "-1", "--format=%H", commit, "--", relative_path]).stdout.decode("ascii").strip()
        if not FULL_SHA_RE.fullmatch(output):
            raise ProvenanceValidationError(f"Git history has no last-change commit for {relative_path} at {commit}")
        return output


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProvenanceValidationError(message)


def _require_full_sha(value: Any, label: str) -> str:
    _require(isinstance(value, str) and FULL_SHA_RE.fullmatch(value) is not None,
             f"{label} must be a lowercase full commit hash")
    return value


def _require_commit(git: GitWitness, value: Any, label: str) -> str:
    commit = _require_full_sha(value, label)
    _require(git.commit_exists(commit), f"{label} commit is absent from local history: {commit}")
    return commit


def _repo_path(repo_root: Path, relative_path: Any, label: str) -> Path:
    _require(isinstance(relative_path, str) and relative_path, f"{label} path must be a non-empty string")
    pure_path = PurePosixPath(relative_path)
    _require(not pure_path.is_absolute() and ".." not in pure_path.parts, f"{label} path must stay within the repo")
    resolved_root = repo_root.resolve()
    resolved_path = (resolved_root / Path(*pure_path.parts)).resolve()
    _require(resolved_path.is_relative_to(resolved_root), f"{label} path escapes the repo")
    _require(resolved_path.is_file(), f"{label} file is missing: {relative_path}")
    return resolved_path


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceValidationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _hash_bytes(content: bytes, mode: str) -> str:
    if mode == "lf_normalized_sha256":
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def _validate_hash_record(record: Any, label: str) -> None:
    _require(isinstance(record, dict), f"{label} must be an object")
    _require(isinstance(record.get("path"), str) and record["path"],
             f"{label} path must be a non-empty string")
    expected_hash = record.get("sha256")
    _require(isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash) is not None,
             f"{label} sha256 must be a lowercase 64-character digest")
    mode = record.get("hash_mode")
    _require(mode in HASH_MODES, f"{label} hash_mode must be raw_sha256 or lf_normalized_sha256")


def _validate_hashed_file(repo_root: Path, record: Any, label: str) -> Path:
    _validate_hash_record(record, label)
    path = _repo_path(repo_root, record.get("path"), label)
    expected_hash = record["sha256"]
    actual_hash = _hash_bytes(path.read_bytes(), record["hash_mode"])
    _require(actual_hash == expected_hash, f"{label} sha256 mismatch: expected {expected_hash}, got {actual_hash}")
    return path


def _validate_snapshot_blob(git: GitWitness, commit: str, record: dict[str, Any], label: str) -> bytes:
    blob = git.blob(commit, record["path"])
    actual_hash = _hash_bytes(blob, record["hash_mode"])
    _require(actual_hash == record["sha256"],
             f"{label} is not bound to source_state_commit {commit}: expected {record['sha256']}, got {actual_hash}")
    return blob


def _validate_pipeline_values(pipeline: dict[str, str]) -> None:
    for key, value in pipeline.items():
        if ".signature." in key:
            _require(SIGNATURE_RE.fullmatch(value) is not None,
                     f"pipeline signature value must be an unsigned decimal integer: {key}")
            _require(int(value) <= (2**64 - 1), f"pipeline signature value exceeds uint64: {key}")
        elif "_psnr." in key:
            _require(PSNR_RE.fullmatch(value) is not None,
                     f"pipeline PSNR value must use fixed four-decimal format: {key}")
        else:
            _require(SHA256_RE.fullmatch(value) is not None,
                     f"pipeline frame/hash value must be lowercase SHA-256: {key}")


def _phase3_frame_hash(phase3: Any, clip_name: Any, frame_number: Any, frame_format: Any) -> str:
    _require(isinstance(phase3, dict), "Phase3 golden root must be an object")
    clips = phase3.get("clips")
    _require(isinstance(clips, list), "Phase3 golden clips must be an array")
    matches: list[str] = []
    for clip in clips:
        if not isinstance(clip, dict) or clip.get("name") != clip_name:
            continue
        frames = clip.get("frames")
        _require(isinstance(frames, list), f"Phase3 clip {clip_name} frames must be an array")
        for frame in frames:
            if (
                isinstance(frame, dict)
                and frame.get("frame") == frame_number
                and frame.get("format") == frame_format
                and isinstance(frame.get("sha256"), str)
            ):
                matches.append(frame["sha256"])
    _require(len(matches) == 1, f"Phase3 pair must resolve exactly once: {clip_name} frame {frame_number} {frame_format}")
    _require(SHA256_RE.fullmatch(matches[0]) is not None,
             f"Phase3 frame value must be lowercase SHA-256: {clip_name} frame {frame_number}")
    return matches[0]


def _validate_test_symbols(producer_tests: list[str], source_blobs: list[bytes]) -> None:
    source_text = "\n".join(blob.decode("utf-8", errors="replace") for blob in source_blobs)
    for test_name in producer_tests:
        parts = test_name.split(".")
        _require(len(parts) == 2 and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts),
                 f"generator producer test name is malformed: {test_name}")
        suite, name = parts
        pattern = re.compile(rf"\bTEST(?:_F)?\s*\(\s*{re.escape(suite)}\s*,\s*{re.escape(name)}\s*\)")
        _require(pattern.search(source_text) is not None,
                 f"generator producer test is absent from source_state_commit: {test_name}")


def _validate_toolchain(toolchain: dict[str, Any], evidence_blob: bytes) -> None:
    os_name = toolchain.get("os")
    qt_version = toolchain.get("qt")
    compiler = toolchain.get("compiler")
    _require(isinstance(os_name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", os_name) is not None,
             "generator toolchain os must be recorded")
    _require(isinstance(qt_version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", qt_version) is not None,
             "generator toolchain qt must be a semantic version")
    compiler_match = re.fullmatch(r"MinGW ([0-9]+)\.([0-9]+)", compiler or "")
    _require(compiler_match is not None, "generator toolchain compiler must identify a MinGW major.minor version")
    evidence = evidence_blob.decode("utf-8", errors="replace")
    _require(re.search(rf"runs-on:\s*{re.escape(os_name)}\b", evidence) is not None,
             f"generator toolchain os is not evidenced at source_state_commit: {os_name}")
    _require(re.search(rf"QT_VERSION:\s*{re.escape(qt_version)}\b", evidence) is not None,
             f"generator Qt version is not evidenced at source_state_commit: {qt_version}")
    mingw_token = f"tools_mingw{compiler_match.group(1)}{compiler_match.group(2)}0"
    _require(mingw_token in evidence,
             f"generator compiler is not evidenced at source_state_commit: {compiler}")


def _validate_approval_witness(
    repo_root: Path,
    git: GitWitness,
    source_state: str,
    record: Any,
    *,
    label: str,
    kind: str,
    artifact_path: str,
    artifact_sha256: str,
    reviewed_range: str,
    reviewed_key_count: int,
) -> None:
    witness_path = _validate_hashed_file(repo_root, record, label)
    _validate_snapshot_blob(git, source_state, record, label)
    witness = _load_json(witness_path, label)
    _require(isinstance(witness, dict) and witness.get("schema_version") == 1,
             f"{label} must use schema_version 1")
    _require(witness.get("kind") == kind, f"{label} kind must be {kind}")
    _require(witness.get("verdict") == "APPROVE", f"{label} verdict must be APPROVE")
    _require(witness.get("artifact_path") == artifact_path,
             f"{label} artifact_path does not match the reviewed artifact")
    _require(witness.get("artifact_sha256") == artifact_sha256,
             f"{label} artifact_sha256 does not match the reviewed artifact")
    _require(witness.get("reviewed_range") == reviewed_range,
             f"{label} reviewed_range does not match the declared review range")
    _require(reviewed_range.count("..") == 1, f"{label} reviewed_range must be base..head")
    base_text, head_text = reviewed_range.split("..")
    base = _require_commit(git, base_text, f"{label} range base")
    head = _require_commit(git, head_text, f"{label} range head")
    _require(git.is_ancestor(base, head), f"{label} review range is not ancestral")
    _require(git.is_ancestor(head, source_state),
             f"{label} review head is not an ancestor of source_state_commit")

    reviewer = witness.get("reviewer")
    _require(reviewer == "layibabalola",
             f"{label} reviewer must be the repository owner layibabalola")
    _require(witness.get("reviewer_role") == "repository_owner",
             f"{label} reviewer_role must be repository_owner")
    reviewed_head = _require_commit(git, witness.get("reviewed_head"), f"{label} reviewed_head")
    _require(reviewed_head == head, f"{label} reviewed_head must equal the review range head")

    source = witness.get("source")
    expected_source_fields = {
        "kind", "repository", "pull_request", "comment_id", "url", "created_at",
        "author_association", "body_sha256",
    }
    _require(isinstance(source, dict) and set(source) == expected_source_fields,
             f"{label} source must contain the exact GitHub authority fields")
    _require(source.get("kind") == "github_issue_comment",
             f"{label} source kind must be github_issue_comment")
    # The record may spell the repository either way (2026-10 transfer; repository id 1169162800).
    _require(is_repository_alias(source.get("repository")),
             f"{label} source repository must be a known name of this repository")
    pull_request = source.get("pull_request")
    comment_id = source.get("comment_id")
    _require(isinstance(pull_request, int) and not isinstance(pull_request, bool) and pull_request > 0,
             f"{label} source pull_request must be a positive integer")
    _require(isinstance(comment_id, int) and not isinstance(comment_id, bool) and comment_id > 0,
             f"{label} source comment_id must be a positive integer")
    expected_url = (
        f"https://github.com/{source['repository']}/pull/{pull_request}"
        f"#issuecomment-{comment_id}"
    )
    _require(source.get("url") == expected_url,
             f"{label} source URL must exactly bind pull_request and comment_id")
    _require(isinstance(source.get("created_at"), str)
             and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", source["created_at"]) is not None,
             f"{label} source created_at must be an RFC3339 UTC timestamp")
    _require(source.get("author_association") == "OWNER",
             f"{label} source author_association must be OWNER")
    _require(isinstance(source.get("body_sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", source["body_sha256"]) is not None,
             f"{label} source body_sha256 must bind the exact approval text")

    scope = witness.get("scope")
    _require(isinstance(scope, dict)
             and set(scope) == {"key_count", "provider_authority", "automatic_launch_gate"},
             f"{label} scope must contain the exact authority-boundary fields")
    _require(scope.get("key_count") == reviewed_key_count,
             f"{label} scope key_count does not match the reviewed artifact")
    _require(scope.get("provider_authority") is False,
             f"{label} must not grant provider authority")
    _require(scope.get("automatic_launch_gate") == "CLOSED",
             f"{label} automatic launch gate must remain CLOSED")
    verified_claim = VERIFIED_APPROVAL_CLAIMS.get(kind)
    _require(verified_claim is not None,
             f"{label} has no independently verified trusted approval claim registered")
    _require(witness == verified_claim,
             f"{label} must equal the complete independently verified approval claim")


def _validate_history(
    git: GitWitness,
    artifact: dict[str, Any],
    source_state: str,
    external_observations: list[dict[str, Any]],
    phase3_record: dict[str, Any],
) -> None:
    artifact_path = artifact["path"]
    introduced_by = _require_commit(git, artifact.get("introduced_by"), "pipeline artifact introduced_by")
    _require(git.is_ancestor(introduced_by, source_state),
             "pipeline artifact introduced_by is not an ancestor of source_state_commit")
    introduced_blob_hash = _hash_bytes(git.blob(introduced_by, artifact_path), artifact["hash_mode"])
    _require(introduced_blob_hash == artifact["sha256"],
             "pipeline artifact introduced_by does not contain the declared artifact blob")
    _require(git.last_change(source_state, artifact_path) == introduced_by,
             "pipeline artifact introduced_by is not the last artifact-changing commit at source_state_commit")
    _validate_snapshot_blob(git, source_state, artifact, "pipeline artifact")

    for index, observation in enumerate(external_observations):
        head = _require_commit(
            git,
            observation.get("head_sha"),
            f"pipeline external_observations[{index}] head_sha",
        )
        run_id = observation["run_id"]
        expected_urls = alias_urls(f"https://github.com/{{repository}}/actions/runs/{run_id}")
        _require(observation.get("url") in expected_urls,
                 f"pipeline external_observations[{index}] URL must exactly match its hosted run_id")
        observed_artifact_hash = _hash_bytes(git.blob(head, artifact_path), artifact["hash_mode"])
        _require(observed_artifact_hash == artifact["sha256"],
                 f"pipeline external_observations[{index}] artifact blob does not match")
        _require(git.is_ancestor(introduced_by, head),
                 f"pipeline external_observations[{index}] predates artifact introduction")
        _require(git.is_ancestor(head, source_state),
                 f"pipeline external_observations[{index}] is not ancestral to source state")

    phase3_review = phase3_record["review"]
    if phase3_review.get("status") in {"ratified", "git_integrated_external_review_asserted"}:
        reviewed_range = phase3_review.get("reviewed_range")
        _require(isinstance(reviewed_range, str) and reviewed_range.count("..") == 1,
                 "Phase3 reviewed_range must be a full base..head range")
        reviewed_base_text, reviewed_head_text = reviewed_range.split("..")
        reviewed_base = _require_commit(git, reviewed_base_text, "Phase3 reviewed range base")
        reviewed_head = _require_commit(git, reviewed_head_text, "Phase3 reviewed range head")
        _require(git.parents(reviewed_head) == [reviewed_base],
                 "Phase3 reviewed range head must be the direct child of its declared base")
        _require(git.last_change(source_state, phase3_record["path"]) == reviewed_head,
                 "Phase3 reviewed range head is not the last Phase3 artifact-changing commit")
        reviewed_blob_hash = _hash_bytes(
            git.blob(reviewed_head, phase3_record["path"]), phase3_record["hash_mode"]
        )
        _require(reviewed_blob_hash == phase3_record["sha256"],
                 "Phase3 reviewed range head does not contain the declared Phase3 artifact")

        landed = _require_commit(git, phase3_review.get("landed_commit"), "Phase3 landed_commit")
        _require(git.is_ancestor(reviewed_head, landed),
                 "Phase3 reviewed range head is not contained by landed_commit")
        _require(git.is_ancestor(landed, source_state),
                 "Phase3 landed_commit is not an ancestor of source_state_commit")
        landed_parents = git.parents(landed)
        _require(len(landed_parents) >= 2, "Phase3 landed_commit must be the merge that integrated reviewed history")
        _require(not git.is_ancestor(reviewed_head, landed_parents[0]),
                 "Phase3 landed_commit first parent already contained reviewed history")
        _require(any(git.is_ancestor(reviewed_head, parent) for parent in landed_parents[1:]),
                 "Phase3 landed_commit has no non-first parent containing reviewed history")
        landed_blob_hash = _hash_bytes(git.blob(landed, phase3_record["path"]), phase3_record["hash_mode"])
        _require(landed_blob_hash == phase3_record["sha256"],
                 "Phase3 landed_commit does not contain the declared Phase3 artifact")


def validate(
    repo_root: Path,
    manifest_path: Path | None = None,
    git_witness: GitWitness | None = None,
) -> None:
    """Validate a repository's tracked pipeline-golden provenance contract."""

    repo_root = repo_root.resolve()
    if git_witness is None:
        _require((repo_root / ".git").exists(), "Git history is required to validate provenance claims")
        git_witness = SubprocessGitWitness(repo_root)
    manifest_relative = manifest_path or DEFAULT_MANIFEST
    manifest_file = manifest_relative if manifest_relative.is_absolute() else repo_root / manifest_relative
    _require(manifest_file.is_file(), f"provenance manifest is missing: {manifest_file}")
    manifest = _load_json(manifest_file, "provenance manifest")
    _require(isinstance(manifest, dict), "provenance manifest root must be an object")
    _require(manifest.get("schema_version") == 1, "provenance schema_version must be 1")

    artifact = manifest.get("artifact")
    artifact_path = _validate_hashed_file(repo_root, artifact, "pipeline artifact")
    pipeline = _load_json(artifact_path, "pipeline artifact")
    _require(isinstance(pipeline, dict), "pipeline artifact root must be an object")
    _require(all(isinstance(key, str) and isinstance(value, str) for key, value in pipeline.items()),
             "pipeline artifact keys and values must be strings")
    _validate_pipeline_values(pipeline)
    _require(artifact.get("key_count") == len(pipeline),
             f"pipeline artifact key_count mismatch: expected {artifact.get('key_count')}, got {len(pipeline)}")
    _require(artifact.get("content_state") == "committed_historical_golden",
             "pipeline artifact content_state must identify the committed historical golden")
    _require_full_sha(artifact.get("introduced_by"), "pipeline artifact introduced_by")
    _require("validation_evidence" not in artifact,
             "pipeline validation_evidence claims require a locally bound hosted-run witness")
    external_observations = artifact.get("external_observations")
    _require(isinstance(external_observations, list) and external_observations,
             "pipeline artifact external_observations must be a non-empty array")
    for index, observation in enumerate(external_observations):
        label = f"pipeline external_observations[{index}]"
        _require(isinstance(observation, dict), f"{label} must be an object")
        _require(observation.get("kind") == "hosted_golden_check_claim",
                 f"{label} kind must be hosted_golden_check_claim")
        _require(observation.get("verification") == EXTERNAL_ASSERTION,
                 f"{label} verification must be {EXTERNAL_ASSERTION}")
        _require(isinstance(observation.get("run_id"), int) and observation["run_id"] > 0,
                 f"{label} run_id must be positive")
        _require_full_sha(observation.get("head_sha"), f"{label} head_sha")
        _require(observation.get("conclusion") == "success",
                 f"{label} externally asserted conclusion must be success")
        local_correlation = observation.get("local_git_correlation")
        _require(isinstance(local_correlation, dict), f"{label} local_git_correlation must be an object")
        _require(local_correlation.get("head_commit_present") is True,
                 f"{label} local Git correlation must require head commit presence")
        for field in (
            "artifact_blob_matches",
            "introduced_by_is_ancestor",
            "head_is_ancestor_of_source_state",
        ):
            _require(local_correlation.get(field) is True,
                     f"{label} local Git correlation must require {field}")

    review = manifest.get("review")
    _require(isinstance(review, dict) and review.get("status") in REVIEW_STATUSES,
             "pipeline review status must be ratified or unratified")
    review_scope = review.get("scope")
    _require(isinstance(review_scope, dict), "pipeline review scope must be an object")
    review_keys = review_scope.get("keys")
    _require(isinstance(review_keys, list) and all(isinstance(key, str) for key in review_keys),
             "pipeline review scope keys must be an array of strings")
    _require(set(review_keys).issubset(pipeline), "pipeline review scope names a key absent from the artifact")
    if review.get("status") == "unratified":
        _require(review_scope.get("artifact") == "none" and not review_keys,
                 "unratified pipeline artifact must not claim a reviewed artifact/key scope")
        _require("approval_witness" not in review,
                 "unratified pipeline artifact must not carry an approval witness")
    else:
        _require(review_scope.get("artifact") == "complete" and set(review_keys) == set(pipeline),
                 "ratified pipeline artifact must claim complete artifact/key scope")
        _require(isinstance(review.get("approval_witness"), dict),
                 "ratified pipeline artifact requires a tracked approval witness")

    unknowns = manifest.get("unknowns")
    _require(isinstance(unknowns, dict), "unknowns must be an object")
    for field in (
        "original_generation_run",
        "generator_executable_sha256",
        "runner_image_revision",
    ):
        _require(field in unknowns and unknowns[field] is None,
                 f"unknown provenance field must remain explicit null until evidenced: {field}")
    whole_review_range = unknowns.get("whole_artifact_review_range")
    if review.get("status") == "unratified":
        _require(whole_review_range is None,
                 "unratified pipeline whole_artifact_review_range must remain explicit null")
    else:
        _require(isinstance(whole_review_range, str) and whole_review_range.count("..") == 1,
                 "ratified pipeline whole_artifact_review_range must be a full base..head range")

    generator = manifest.get("generator")
    _require(isinstance(generator, dict), "generator must be an object")
    _require(generator.get("executable") == "pipeline_tests.exe", "generator executable must be pipeline_tests.exe")
    source_state = _require_commit(git_witness, generator.get("source_state_commit"), "generator source_state_commit")
    if review.get("status") == "ratified":
        _validate_approval_witness(
            repo_root,
            git_witness,
            source_state,
            review["approval_witness"],
            label="pipeline whole-artifact approval witness",
            kind="tracked_whole_artifact_approval",
            artifact_path=artifact["path"],
            artifact_sha256=artifact["sha256"],
            reviewed_range=whole_review_range,
            reviewed_key_count=len(pipeline),
        )
    dependency_snapshot = generator.get("dependency_snapshot")
    _require(isinstance(dependency_snapshot, dict), "generator dependency_snapshot must be an object")
    _require(dependency_snapshot.get("complete") is False,
             "generator dependency snapshot must not claim complete dependency closure")
    _require(dependency_snapshot.get("scope") == "listed_generator_sources_and_declared_fixture_receipt_inputs_only",
             "generator dependency snapshot scope must remain explicitly partial")
    exclusions = dependency_snapshot.get("excluded")
    _require(isinstance(exclusions, list) and exclusions,
             "generator dependency snapshot must enumerate excluded dependency classes")
    for arguments_name in ("emit_arguments", "check_arguments"):
        arguments = generator.get(arguments_name)
        _require(isinstance(arguments, list) and arguments and all(isinstance(item, str) for item in arguments),
                 f"generator {arguments_name} must be a non-empty string array")

    toolchain = generator.get("toolchain")
    _require(isinstance(toolchain, dict), "generator toolchain must be an object")
    evidence_record = toolchain.get("evidence")
    _validate_hash_record(evidence_record, "generator toolchain evidence")
    evidence_blob = _validate_snapshot_blob(git_witness, source_state, evidence_record,
                                            "generator toolchain evidence")
    _validate_toolchain(toolchain, evidence_blob)

    sources = generator.get("sources")
    _require(isinstance(sources, list) and sources, "generator sources must be a non-empty array")
    source_blobs: list[bytes] = []
    for index, source in enumerate(sources):
        _validate_hashed_file(repo_root, source, f"generator source[{index}]")
        source_blobs.append(_validate_snapshot_blob(git_witness, source_state, source,
                                                    f"generator source[{index}]"))
    producer_tests = generator.get("producer_tests")
    _require(isinstance(producer_tests, list) and producer_tests and all(isinstance(test, str) for test in producer_tests),
             "generator producer_tests must be a non-empty string array")
    _require(len(producer_tests) == len(set(producer_tests)), "generator producer_tests must be unique")
    _validate_test_symbols(producer_tests, source_blobs)
    source_text = "\n".join(blob.decode("utf-8", errors="replace") for blob in source_blobs)
    for required_argument in ("--hash-output", "--check-golden"):
        _require(required_argument in source_text,
                 f"generator argument is absent from source_state_commit: {required_argument}")

    inputs = manifest.get("inputs")
    _require(isinstance(inputs, list) and inputs, "inputs must be a non-empty array")
    input_roles: set[str] = set()
    for index, input_record in enumerate(inputs):
        _require(isinstance(input_record, dict) and isinstance(input_record.get("role"), str),
                 f"input[{index}] role must be recorded")
        input_roles.add(input_record["role"])
        _validate_hashed_file(repo_root, input_record, f"input[{index}]")
        _validate_snapshot_blob(git_witness, source_state, input_record, f"input[{index}]")
    _require({"fixture", "receipt"}.issubset(input_roles), "inputs must include fixture and receipt provenance")

    cross_golden = manifest.get("cross_golden")
    _require(isinstance(cross_golden, dict), "cross_golden must be an object")
    phase3_record = cross_golden.get("phase3")
    phase3_path = _validate_hashed_file(repo_root, phase3_record, "Phase3 artifact")
    phase3 = _load_json(phase3_path, "Phase3 artifact")
    phase3_review = phase3_record.get("review") if isinstance(phase3_record, dict) else None
    _require(isinstance(phase3_review, dict) and phase3_review.get("status") in PHASE3_REVIEW_STATUSES,
             "Phase3 review status must be unratified, ratified, or git_integrated_external_review_asserted")
    phase3_scope = phase3_review.get("scope")
    _require(isinstance(phase3_scope, list) and all(isinstance(key, str) for key in phase3_scope),
             "Phase3 review scope must be an array of strings")
    if phase3_review.get("status") == "git_integrated_external_review_asserted":
        external_review = phase3_review.get("external_review_witness")
        _require(isinstance(external_review, dict),
                 "Phase3 external review witness must be an object")
        _require(external_review.get("verification") == EXTERNAL_ASSERTION,
                 f"Phase3 external review verification must be {EXTERNAL_ASSERTION}")
        _require("tracked_approval_witness" in external_review
                 and external_review["tracked_approval_witness"] is None,
                 "Phase3 tracked approval witness must remain explicit null until locally bound")
        _require(isinstance(external_review.get("reason"), str) and external_review["reason"],
                 "Phase3 external review witness must explain the local verification boundary")
        _require("approval_witness" not in phase3_review,
                 "externally asserted Phase3 review must not claim a tracked approval witness")
    elif phase3_review.get("status") == "ratified":
        _require("external_review_witness" not in phase3_review,
                 "ratified Phase3 review must not retain an external-only review witness")
        _validate_approval_witness(
            repo_root,
            git_witness,
            source_state,
            phase3_review.get("approval_witness"),
            label="Phase3 approval witness",
            kind="tracked_phase3_approval",
            artifact_path=phase3_record["path"],
            artifact_sha256=phase3_record["sha256"],
            reviewed_range=phase3_review.get("reviewed_range"),
            reviewed_key_count=len(phase3_scope),
        )
    else:
        _require(not phase3_scope,
                 "unratified Phase3 artifact must not claim a reviewed key scope")
        _require("external_review_witness" not in phase3_review and "approval_witness" not in phase3_review,
                 "unratified Phase3 artifact must not carry a review witness")

    pairs = cross_golden.get("pairs")
    _require(isinstance(pairs, list), "cross_golden pairs must be an array")
    pair_keys = [pair.get("pipeline_key") for pair in pairs if isinstance(pair, dict)]
    _require(len(pair_keys) == len(pairs), "every cross-golden pair must be an object with pipeline_key")
    _require(len(pair_keys) == len(set(pair_keys)), "cross-golden pipeline keys must be unique")
    _require(set(pair_keys) == EXPECTED_FULL16_KEYS,
             "cross-golden pairs must cover both and only pipeline full16 frame keys")
    _require(set(phase3_scope) == EXPECTED_FULL16_KEYS,
             "Phase3 review scope must cover both and only pipeline full16 frame keys")

    both_ratified = review.get("status") == "ratified" and phase3_review.get("status") == "ratified"
    for pair in pairs:
        pipeline_key = pair["pipeline_key"]
        _require(pipeline_key in pipeline, f"cross-golden key is absent from pipeline artifact: {pipeline_key}")
        phase3_hash = _phase3_frame_hash(
            phase3,
            pair.get("phase3_clip"),
            pair.get("phase3_frame"),
            pair.get("phase3_format"),
        )
        actual_status = "aligned" if pipeline[pipeline_key] == phase3_hash else "known_mismatch"
        _require(pair.get("status") in PAIR_STATUSES, f"cross-golden pair status is invalid: {pipeline_key}")
        _require(pair.get("status") == actual_status,
                 f"cross-golden pair status drift for {pipeline_key}: declared {pair.get('status')}, actual {actual_status}")
        if both_ratified:
            _require(actual_status == "aligned",
                     f"ratified pipeline and Phase3 full16 hashes must agree: {pipeline_key}")

    _validate_snapshot_blob(git_witness, source_state, phase3_record, "Phase3 artifact")
    _validate_history(git_witness, artifact, source_state, external_observations, phase3_record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        validate(args.repo_root, args.manifest)
    except ProvenanceValidationError as exc:
        print(f"pipeline golden provenance: FAIL: {exc}", file=sys.stderr)
        return 1
    print("pipeline golden provenance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
