from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


VAGUE_SUBJECT_PATTERNS = [
    ("generic brokered checkpoint", re.compile(r"^brokered closeout checkpoint$")),
    ("generic brokered evidence repair", re.compile(r"^brokered closeout evidence repair$")),
    ("work-block checkpoint", re.compile(r"^chore\(closeout\): checkpoint (?P<paths>.+) for (?P<workBlockId>wb-[^ ]+)$")),
    (
        "work-block evidence repair",
        re.compile(r"^chore\(closeout\): repair (?P<paths>.+) for (?P<workBlockId>wb-[^ ]+) before (?P<reason>.+)$"),
    ),
    (
        "work-block merge",
        re.compile(r"^merge\(closeout\): integrate (?P<workBlockId>wb-[^ ]+) closeout hardening into (?P<target>.+)$"),
    ),
]


WORK_BLOCK_RE = re.compile(r"\b(wb-[0-9a-fA-F]+)\b")


@dataclass(frozen=True)
class CommitRow:
    hash: str
    parents: List[str]
    date: str
    subject: str


def run_git(repo_root: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def classify_vague_subject(subject: str) -> Optional[Dict[str, str]]:
    for family, pattern in VAGUE_SUBJECT_PATTERNS:
        match = pattern.match(subject)
        if match:
            return {"family": family, **{key: value for key, value in match.groupdict().items() if value}}
    return None


def is_vague_subject(subject: str) -> bool:
    return classify_vague_subject(subject) is not None


def extract_work_block_id(subject: str) -> Optional[str]:
    classified = classify_vague_subject(subject)
    if classified and classified.get("workBlockId"):
        return classified["workBlockId"]
    match = WORK_BLOCK_RE.search(subject)
    return match.group(1) if match else None


def closeout_detail_for_subject(subject: str) -> str:
    classified = classify_vague_subject(subject) or {}
    family = classified.get("family")
    if family == "work-block checkpoint":
        return "Closeout: checkpoint %s." % classified.get("paths", "repo-managed updates")
    if family == "work-block evidence repair":
        return "Closeout: repair %s before %s." % (
            classified.get("paths", "closeout evidence"),
            classified.get("reason", "final push"),
        )
    if family == "work-block merge":
        return "Closeout: integrate work block %s into %s after clean validation." % (
            classified.get("workBlockId", "unknown"),
            classified.get("target", "target"),
        )
    if family == "generic brokered checkpoint":
        return "Closeout: checkpoint owned work."
    if family == "generic brokered evidence repair":
        return "Closeout: repair closeout evidence."
    return "Closeout: original generated subject was `%s`." % subject


def parse_log_rows(raw: str) -> List[CommitRow]:
    rows: List[CommitRow] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 3)
        if len(parts) != 4:
            continue
        rows.append(
            CommitRow(
                hash=parts[0],
                parents=[parent for parent in parts[1].split(" ") if parent],
                date=parts[2],
                subject=parts[3],
            )
        )
    return rows


def commit_log(repo_root: Path, ref: str) -> List[CommitRow]:
    raw = run_git(repo_root, ["log", ref, "--date=iso-strict", "--format=%H%x1f%P%x1f%cI%x1f%s"])
    return parse_log_rows(raw)


def side_branch_subjects(repo_root: Path, first_parent: str, second_parent: str) -> List[Dict[str, str]]:
    raw = run_git(repo_root, ["log", "--reverse", "--format=%H%x1f%s", "%s..%s" % (first_parent, second_parent)])
    subjects: List[Dict[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\x1f", 1)
        if len(parts) != 2:
            continue
        subjects.append({"hash": parts[0], "subject": parts[1]})
    return subjects


def changed_paths(repo_root: Path, commit_hash: str, *, limit: int = 20) -> List[str]:
    # `-z`: without it git quotes a non-ASCII path, which no prefix test matches (bus de09cae).
    raw = run_git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-z", "-r", commit_hash])
    return [part for part in raw.split("\0") if part][:limit]


def changed_paths_for_row(repo_root: Path, row: CommitRow, *, limit: int = 40) -> List[str]:
    if len(row.parents) >= 2:
        raw = run_git(repo_root, ["diff", "--name-only", "-z", row.parents[0], row.parents[1]])
        paths = [part for part in raw.split("\0") if part]
        if paths:
            return paths[:limit]
    return changed_paths(repo_root, row.hash, limit=limit)


def commit_subject(repo_root: Path, commit_hash: str) -> str:
    try:
        return run_git(repo_root, ["show", "--no-patch", "--format=%s", commit_hash]).strip()
    except subprocess.CalledProcessError:
        return ""


def commit_row(repo_root: Path, commit_hash: str) -> Optional[CommitRow]:
    try:
        raw = run_git(repo_root, ["show", "--no-patch", "--date=iso-strict", "--format=%H%x1f%P%x1f%cI%x1f%s", commit_hash])
    except subprocess.CalledProcessError:
        return None
    rows = parse_log_rows(raw)
    return rows[0] if rows else None


def show_json_at_commit(repo_root: Path, commit_hash: str, path: str) -> Optional[Dict[str, Any]]:
    try:
        raw = run_git(repo_root, ["show", "%s:%s" % (commit_hash, path)])
    except subprocess.CalledProcessError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def evidence_payload_for_commit(repo_root: Path, commit_hash: str, paths: Sequence[str]) -> Dict[str, Any]:
    for path in paths:
        if path.endswith("/closeout.json") or path.endswith("\\closeout.json"):
            payload = show_json_at_commit(repo_root, commit_hash, path)
            if payload:
                return payload
    for path in paths:
        if path.endswith(".json") and ".closeout-evidence/" in path.replace("\\", "/"):
            payload = show_json_at_commit(repo_root, commit_hash, path)
            if payload:
                return payload
    return {}


def readable_label(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    label = raw.replace("\\", "/").rsplit("/", 1)[-1]
    label = re.sub(r"^codex[-/]", "", label)
    label = re.sub(r"^wb-", "", label)
    label = re.sub(r"-?20\d{6,8}$", "", label)
    label = re.sub(r"\b[0-9a-fA-F]{8,}\b", "", label)
    label = label.replace("_", "-")
    words = [word for word in re.split(r"[-\s]+", label) if word]
    if not words:
        return ""
    canonical = {
        "ci": "CI",
        "dng": "DNG",
        "gui": "GUI",
        "mlv": "MLV",
        "qt": "Qt",
        "ui": "UI",
    }
    return " ".join(canonical.get(word.lower(), word.lower()) for word in words)


def path_scope(paths: Sequence[str]) -> str:
    normalized = [path.replace("\\", "/") for path in paths]
    if not normalized:
        return "repo"
    if all(path.startswith(".closeout-evidence/") for path in normalized):
        return "closeout"
    if any(path.startswith("platform/qt/") for path in normalized):
        return "qt"
    if any(path.startswith("src/dng/") or "/dng" in path.lower() for path in normalized):
        return "dng"
    if any(path.startswith("tools/agent-bridge/") for path in normalized):
        return "bridge"
    if any(path.startswith("tools/closeout/") or path.startswith("tools/repo_hygiene/") for path in normalized):
        return "closeout"
    if any(path.startswith("tools/repo-hygiene/") for path in normalized):
        return "closeout"
    if any(path.startswith(".github/") or path.startswith("tests/") for path in normalized):
        return "tests"
    if all(path.startswith("docs/") or path.startswith(".claude/analysis/") or path in {"AGENTS.md", "CLAUDE.md"} for path in normalized):
        return "docs"
    return normalized[0].split("/", 1)[0] if "/" in normalized[0] else "repo"


def has_any_path(paths: Sequence[str], *needles: str) -> bool:
    normalized = [path.replace("\\", "/") for path in paths]
    return any(any(needle in path for needle in needles) for path in normalized)


def summarize_path_delta(paths: Sequence[str]) -> str:
    normalized = [path.replace("\\", "/") for path in paths]
    filenames = {Path(path).name for path in normalized}
    if normalized and all(path.startswith(".closeout-evidence/") for path in normalized):
        return "closeout evidence"
    if {"MainWindow.cpp", "MainWindow.h", "MainWindow.ui"} & filenames:
        return "Qt playback controls"
    if "dng.c" in filenames or "dng.h" in filenames:
        return "DNG export metadata"
    if filenames and filenames.issubset({"AGENTS.md", "CLAUDE.md"}):
        return "agent closeout guidance"
    if any(path.startswith("tools/agent-bridge/") for path in normalized):
        return "agent bridge workflow"
    if "commit_message_rewrite_plan.py" in filenames:
        return "commit message rewrite planning"
    if "brokered_closeout.py" in filenames or "test_brokered_closeout.py" in filenames:
        return "brokered closeout workflow"
    if "Invoke-CloseoutCli.ps1" in filenames:
        return "closeout CLI invocation"
    if any(path.startswith("tools/repo-hygiene/") for path in normalized):
        return "repo hygiene tooling"
    if any(path.startswith("tests/") for path in normalized):
        return "test coverage"
    if any(path.startswith(".github/workflows/") for path in normalized):
        return "CI workflow"
    if all(path.startswith("docs/") for path in normalized):
        return "documentation"
    if filenames:
        shown = sorted(filenames)[:3]
        if len(shown) == 1:
            return shown[0]
        return ", ".join(shown[:-1]) + ", and " + shown[-1]
    return "repo-managed files"


def detailed_subject_from_paths(paths: Sequence[str], *, family: str = "") -> Optional[str]:
    normalized = [path.replace("\\", "/") for path in paths]
    subject_paths = [path for path in normalized if not path.startswith(".closeout-evidence/")] or normalized
    filenames = {Path(path).name for path in normalized}
    subject_filenames = {Path(path).name for path in subject_paths}
    closeout_core = {
        "brokered_closeout.py",
        "test_brokered_closeout.py",
        "work_block_cli.py",
        "closeout.config.json",
    }
    if "commit_message_rewrite_plan.py" in filenames:
        return "closeout: generate detailed rewrite map from commit deltas"
    if closeout_core & filenames and any(path.startswith("tools/closeout/") for path in normalized):
        return "closeout: wire brokered work-block scripts into closeout tooling"
    if closeout_core & filenames and {"AGENTS.md", "CLAUDE.md"} & filenames:
        return "closeout: update broker policy, tests, and agent guidance"
    if closeout_core & filenames:
        return "closeout: harden brokered closeout workflow and tests"
    if any(path.startswith("tools/repo-hygiene/") for path in normalized):
        return "closeout: add repo hygiene policy, CLI, and tests"
    if any(path.startswith("tools/agent-bridge/") for path in normalized):
        if any("watcher" in path or "bridge" in path for path in normalized):
            return "bridge: harden agent bridge watcher and lifecycle scripts"
        return "bridge: update agent bridge workflow scripts"
    if {"MainWindow.cpp", "MainWindow.h", "MainWindow.ui"} & filenames and has_any_path(normalized, "ReceiptApplier", "ReceiptSafety"):
        return "qt: apply receipt safeguards to playback controls"
    if {"MainWindow.cpp", "MainWindow.h", "MainWindow.ui"} & filenames and ("dng.c" in filenames or "dng.h" in filenames):
        return "qt: align playback controls with DNG export metadata"
    if {"MainWindow.cpp", "MainWindow.h", "MainWindow.ui"} & filenames:
        return "qt: refine playback controls and scaling behavior"
    if "dualiso.c" in filenames or "dualiso.h" in filenames:
        return "tests: add dual ISO pipeline coverage"
    if "dng.c" in filenames or "dng.h" in filenames:
        return "dng: update export metadata and raw-level handling"
    if family == "work-block merge" and {"AGENTS.md", "CLAUDE.md"} & subject_filenames:
        return "closeout: integrate agent closeout guidance"
    if family == "work-block merge" and any(path.startswith("docs/") or path.startswith("CLOSEOUT-") for path in subject_paths):
        if {"AGENTS.md", "CLAUDE.md"} & subject_filenames:
            return "closeout: integrate dashboard docs and agent guidance"
        return "closeout: integrate closeout reference documentation"
    if any(path.startswith(".github/workflows/") for path in normalized) and any(path.startswith("tests/") for path in normalized):
        return "tests: add CI coverage for repo test scaffold"
    if any(path.startswith("tests/") for path in normalized):
        return "tests: expand targeted regression coverage"
    if filenames and filenames.issubset({"AGENTS.md", "CLAUDE.md"}):
        return "docs: update agent closeout guidance"
    if all(path.startswith("docs/") or path.startswith(".claude/analysis/") for path in normalized):
        return "docs: update closeout and playback investigation notes"
    if family == "work-block merge":
        summary = summarize_path_delta(paths)
        return "%s: integrate %s" % (path_scope(paths), summary)
    return None


def evidence_subject(label: str) -> str:
    clean_label = label.strip() or "closeout"
    if clean_label.endswith(" evidence"):
        return "closeout: record %s" % clean_label
    return "closeout: record %s evidence" % clean_label


def content_subject_from_paths(paths: Sequence[str], *, family: str = "") -> Dict[str, str]:
    subject_paths = [path for path in paths if not path.replace("\\", "/").startswith(".closeout-evidence/")] or list(paths)
    scope = path_scope(subject_paths)
    summary = summarize_path_delta(subject_paths)
    detailed_subject = detailed_subject_from_paths(paths, family=family)
    if detailed_subject:
        return {
            "subject": detailed_subject,
            "confidence": "medium" if paths else "low",
            "reason": "generated from detailed path delta",
        }
    if family == "work-block merge":
        return {
            "subject": "%s: integrate %s" % (scope, summary),
            "confidence": "low",
            "reason": "generated from merge delta paths against the first parent",
        }
    verb = "update"
    if scope == "tests":
        verb = "add"
    if scope == "docs":
        verb = "document"
    if summary == "closeout evidence":
        verb = "record"
    return {
        "subject": "%s: %s %s" % (scope, verb, summary),
        "confidence": "medium" if paths else "low",
        "reason": "generated from commit changed paths",
    }


def content_subject_from_feature_head(repo_root: Path, feature_head: str, *, depth: int = 0) -> Dict[str, Any]:
    row = commit_row(repo_root, feature_head)
    if not row:
        return {"subject": "closeout: repair nested closeout evidence", "confidence": "low", "reason": "feature head was unavailable"}
    if row.subject and not is_vague_subject(row.subject):
        return {"subject": row.subject, "confidence": "medium", "reason": "reused non-generated feature-head subject"}
    paths = changed_paths_for_row(repo_root, row)
    evidence_only = bool(paths) and all(path.replace("\\", "/").startswith(".closeout-evidence/") for path in paths)
    if evidence_only and depth < 4:
        evidence = evidence_payload_for_commit(repo_root, row.hash, paths)
        nested_head = str(evidence.get("featureHead") or "")
        if nested_head and nested_head != feature_head:
            return content_subject_from_feature_head(repo_root, nested_head, depth=depth + 1)
    classified = classify_vague_subject(row.subject) or {}
    return content_subject_from_paths(paths, family=str(classified.get("family") or ""))


def path_detail_groups(paths: Sequence[str]) -> List[str]:
    normalized = [path.replace("\\", "/") for path in paths]
    groups: List[str] = []
    def add(label: str, selected: Sequence[str]) -> None:
        if not selected:
            return
        shown = list(selected)[:4]
        suffix = "" if len(selected) <= 4 else " and %d more" % (len(selected) - 4)
        groups.append("%s: %s%s" % (label, ", ".join(shown), suffix))

    add("Closeout evidence", [path for path in normalized if path.startswith(".closeout-evidence/")])
    add("Closeout broker/tooling", [path for path in normalized if path.startswith("tools/closeout/") or path.startswith("tools/repo_hygiene/") or path == "closeout.config.json"])
    add("Agent guidance", [path for path in normalized if path in {"AGENTS.md", "CLAUDE.md"}])
    add("Agent bridge", [path for path in normalized if path.startswith("tools/agent-bridge/")])
    add("Qt UI/playback", [path for path in normalized if path.startswith("platform/qt/")])
    add("Raw/DNG pipeline", [path for path in normalized if path.startswith("src/")])
    add("Tests/CI", [path for path in normalized if path.startswith("tests/") or path.startswith(".github/workflows/")])
    add("Docs/analysis", [path for path in normalized if path.startswith("docs/") or path.startswith(".claude/analysis/")])
    covered_prefixes = (
        ".closeout-evidence/",
        "tools/closeout/",
        "tools/repo_hygiene/",
        "tools/agent-bridge/",
        "platform/qt/",
        "src/",
        "tests/",
        ".github/workflows/",
        "docs/",
        ".claude/analysis/",
    )
    covered_exact = {"AGENTS.md", "CLAUDE.md", "closeout.config.json"}
    other = [path for path in normalized if path not in covered_exact and not any(path.startswith(prefix) for prefix in covered_prefixes)]
    add("Other changed paths", other)
    return groups


def detailed_body_lines(row: CommitRow, paths: Sequence[str], proposal: Dict[str, Any], work_block_id: Optional[str]) -> List[str]:
    lines = [
        "Replaces generated subject: %s" % row.subject,
        "Inference source: %s." % str(proposal.get("reason") or "commit history and changed paths"),
    ]
    if work_block_id:
        lines.append("Work block: %s." % work_block_id)
    source = proposal.get("source") if isinstance(proposal.get("source"), dict) else {}
    if source:
        if source.get("branch"):
            lines.append("Source branch: %s." % source["branch"])
        if source.get("featureHead"):
            lines.append("Feature head: %s." % str(source["featureHead"])[:12])
        if source.get("targetHead"):
            lines.append("Target head: %s." % str(source["targetHead"])[:12])
    lines.append(closeout_detail_for_subject(row.subject))
    groups = path_detail_groups(paths)
    if groups:
        lines.append("Changed areas:")
        lines.extend("- %s" % group for group in groups[:8])
    return lines


def proposal_with_message(row: CommitRow, paths: Sequence[str], proposal: Dict[str, Any], work_block_id: Optional[str]) -> Dict[str, Any]:
    detailed = dict(proposal)
    body = detailed_body_lines(row, paths, detailed, work_block_id)
    detailed["bodyLines"] = body
    detailed["message"] = str(detailed.get("subject") or "").strip() + "\n\n" + "\n".join(body)
    return detailed


def subject_from_delta(repo_root: Path, row: CommitRow, paths: Sequence[str]) -> Dict[str, Any]:
    evidence = evidence_payload_for_commit(repo_root, row.hash, paths)
    evidence_only = bool(paths) and all(path.replace("\\", "/").startswith(".closeout-evidence/") for path in paths)
    branch_label = readable_label(evidence.get("branch"))
    work_block_label = readable_label(evidence.get("workBlockId") or extract_work_block_id(row.subject))
    classified = classify_vague_subject(row.subject) or {}
    family = str(classified.get("family") or "")

    if evidence and evidence_only:
        feature_head = str(evidence.get("featureHead") or "")
        if not branch_label and feature_head:
            content = content_subject_from_feature_head(repo_root, feature_head)
            return {
                "status": "delta_generated",
                "confidence": content["confidence"],
                "subject": content["subject"],
                "reason": "generated from feature-head history referenced by closeout evidence payload",
                "source": {
                    "branch": evidence.get("branch"),
                    "workBlockId": evidence.get("workBlockId"),
                    "featureHead": feature_head,
                    "targetHead": evidence.get("targetHead"),
                },
            }
        summary = summarize_path_delta(paths)
        label = branch_label or work_block_label or summary
        return {
            "status": "delta_generated",
            "confidence": "medium" if branch_label else "low",
            "subject": evidence_subject(label),
            "reason": "generated from closeout evidence payload and changed evidence paths",
            "source": {
                "branch": evidence.get("branch"),
                "workBlockId": evidence.get("workBlockId"),
                "featureHead": evidence.get("featureHead"),
                "targetHead": evidence.get("targetHead"),
            },
        }
    if family == "work-block evidence repair":
        label = readable_label(classified.get("workBlockId")) or work_block_label or "work block"
        return {
            "status": "delta_generated",
            "confidence": "low",
            "subject": "closeout: repair evidence for %s" % label,
            "reason": "generated from work-block evidence repair subject and changed paths",
        }
    content = content_subject_from_paths(paths, family=family)
    return {
        "status": "delta_generated",
        "confidence": content["confidence"],
        "subject": content["subject"],
        "reason": content["reason"],
    }


def merge_context_by_work_block(repo_root: Path, rows: Iterable[CommitRow]) -> Dict[str, Dict[str, Any]]:
    context: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        classified = classify_vague_subject(row.subject)
        if not classified or classified.get("family") != "work-block merge" or len(row.parents) < 2:
            continue
        work_block_id = str(classified["workBlockId"])
        branch_subjects = side_branch_subjects(repo_root, row.parents[0], row.parents[1])
        human_subjects = [
            item for item in branch_subjects if not is_vague_subject(item["subject"])
        ]
        context[work_block_id] = {
            "mergeHash": row.hash,
            "targetHead": row.parents[0],
            "featureHead": row.parents[1],
            "branchSubjects": branch_subjects,
            "humanSubjects": human_subjects,
        }
    return context


def proposed_subject_for_commit(
    row: CommitRow,
    merge_context: Dict[str, Dict[str, Any]],
    *,
    repo_root: Optional[Path] = None,
    paths: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    work_block_id = extract_work_block_id(row.subject)
    if work_block_id and work_block_id in merge_context:
        human_subjects = merge_context[work_block_id].get("humanSubjects", [])
        if len(human_subjects) == 1:
            return {
                "status": "candidate",
                "confidence": "medium",
                "subject": human_subjects[0]["subject"],
                "reason": "single non-generated subject found on the same work-block side branch",
                "sourceCommit": human_subjects[0]["hash"],
            }
        if len(human_subjects) > 1:
            return {
                "status": "multi_subject_draft",
                "confidence": "low",
                "subject": human_subjects[-1]["subject"],
                "reason": "multiple non-generated subjects found on the same work-block side branch; latest was selected as the draft subject",
                "sourceCommit": human_subjects[-1]["hash"],
                "alternates": human_subjects,
            }
    if repo_root is not None and paths is not None:
        return subject_from_delta(repo_root, row, paths)
    return {
        "status": "needs_subject",
        "confidence": "none",
        "subject": None,
        "reason": "no reliable non-generated subject was found in the associated work-block history",
    }


def harmonize_work_block_subjects(entries: List[Dict[str, Any]]) -> None:
    subject_by_work_block: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        work_block_id = entry.get("workBlockId")
        proposal = entry.get("proposal") or {}
        subject = str(proposal.get("subject") or "")
        if not work_block_id or not subject:
            continue
        if entry.get("family") == "work-block checkpoint" and "closeout evidence" not in subject:
            subject_by_work_block[str(work_block_id)] = {"subject": subject, "hash": str(entry.get("hash") or "")}
    for entry in entries:
        work_block_id = entry.get("workBlockId")
        if not work_block_id or str(work_block_id) not in subject_by_work_block:
            continue
        if entry.get("family") not in {"work-block evidence repair", "work-block merge"}:
            continue
        current = str((entry.get("proposal") or {}).get("subject") or "")
        if current and "closeout evidence" not in current and (entry.get("proposal") or {}).get("confidence") != "low":
            continue
        source = subject_by_work_block[str(work_block_id)]
        entry["proposal"] = {
            "status": "delta_generated",
            "confidence": "medium",
            "subject": source["subject"],
            "reason": "reused subject generated from the same work-block checkpoint delta",
            "sourceCommit": source["hash"],
        }


def build_rewrite_plan(repo_root: Path, ref: str) -> Dict[str, Any]:
    rows = commit_log(repo_root, ref)
    vague_rows = [row for row in rows if is_vague_subject(row.subject)]
    merge_context = merge_context_by_work_block(repo_root, vague_rows)
    entries: List[Dict[str, Any]] = []
    for row in sorted(vague_rows, key=lambda item: item.date):
        classified = classify_vague_subject(row.subject) or {}
        paths = changed_paths_for_row(repo_root, row)
        work_block_id = extract_work_block_id(row.subject)
        proposal = proposed_subject_for_commit(row, merge_context, repo_root=repo_root, paths=paths)
        proposal = proposal_with_message(row, paths, proposal, work_block_id)
        entries.append(
            {
                "hash": row.hash,
                "date": row.date,
                "parents": row.parents,
                "family": classified.get("family"),
                "workBlockId": work_block_id,
                "currentSubject": row.subject,
                "changedPaths": paths,
                "proposal": proposal,
                "proposedBodyLines": proposal["bodyLines"],
            }
        )
    harmonize_work_block_subjects(entries)
    for entry in entries:
        row = CommitRow(
            hash=str(entry["hash"]),
            parents=[str(parent) for parent in entry.get("parents", [])],
            date=str(entry["date"]),
            subject=str(entry["currentSubject"]),
        )
        entry["proposal"] = proposal_with_message(
            row,
            [str(path) for path in entry.get("changedPaths", [])],
            entry["proposal"],
            str(entry.get("workBlockId") or "") or None,
        )
        entry["proposedBodyLines"] = entry["proposal"]["bodyLines"]
    family_counts: Dict[str, int] = {}
    status_counts: Dict[str, int] = {}
    for entry in entries:
        family = str(entry.get("family") or "unknown")
        status = str(entry.get("proposal", {}).get("status") or "unknown")
        family_counts[family] = family_counts.get(family, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "schemaVersion": "commit-message-rewrite-plan.v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "ref": ref,
        "vagueCommitCount": len(entries),
        "earliestVagueCommit": entries[0] if entries else None,
        "latestVagueCommit": entries[-1] if entries else None,
        "familyCounts": family_counts,
        "proposalStatusCounts": status_counts,
        "rewriteSafety": {
            "historyRewriteRequired": True,
            "forcePushRequired": True,
            "requiresReviewedMapping": True,
            "allSubjectsGenerated": bool(entries) and all(entry["proposal"].get("subject") for entry in entries),
            "readyForRewrite": bool(entries) and all(entry["proposal"].get("subject") for entry in entries),
        },
        "entries": entries,
    }


def markdown_summary(plan: Dict[str, Any]) -> str:
    lines = [
        "# Commit Message Rewrite Plan",
        "",
        "- Ref: `%s`" % plan["ref"],
        "- Vague commits: %s" % plan["vagueCommitCount"],
        "- Earliest vague commit: `%s` on %s" % (
            (plan.get("earliestVagueCommit") or {}).get("hash", "")[:12],
            (plan.get("earliestVagueCommit") or {}).get("date", ""),
        ),
        "- Ready for rewrite: %s" % str(plan["rewriteSafety"]["readyForRewrite"]).lower(),
        "",
        "## Proposal Status",
        "",
    ]
    for status, count in sorted(plan["proposalStatusCounts"].items()):
        lines.append("- `%s`: %s" % (status, count))
    lines.extend(["", "## Family Counts", ""])
    for family, count in sorted(plan["familyCounts"].items()):
        lines.append("- `%s`: %s" % (family, count))
    lines.extend(
        [
            "",
            "## Review Notes",
            "",
            "Every entry has a generated full commit message: `proposal.subject`, `proposal.bodyLines`, and `proposal.message`.",
            "Review low-confidence `delta_generated` and `multi_subject_draft` entries before feeding the JSON to a rewrite actor.",
            "Do not rewrite or force-push `master` until the generated mapping is approved.",
            "",
        ]
    )
    for entry in plan["entries"]:
        proposal = entry["proposal"]
        lines.extend(
            [
                "### `%s`" % entry["hash"][:12],
                "",
                "- Date: %s" % entry["date"],
                "- Current: %s" % entry["currentSubject"],
                "- Proposed: %s" % proposal.get("subject"),
                "- Status: `%s`" % proposal["status"],
                "- Confidence: `%s`" % proposal.get("confidence"),
                "- Reason: %s" % proposal["reason"],
                "- Changed paths: %s" % (", ".join(entry["changedPaths"][:8]) if entry["changedPaths"] else "(none listed)"),
                "",
                "Proposed body:",
                "",
            ]
        )
        for body_line in proposal.get("bodyLines") or []:
            lines.append("- %s" % body_line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_plan(plan: Dict[str, Any], output_root: Path) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / "rewrite-plan.json"
    markdown_path = output_root / "rewrite-plan.md"
    csv_path = output_root / "rewrite-map.csv"
    json_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown_summary(plan), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "hash",
                "date",
                "family",
                "currentSubject",
                "proposedSubject",
                "proposedBody",
                "proposedMessage",
                "status",
                "confidence",
                "reason",
            ],
        )
        writer.writeheader()
        for entry in plan["entries"]:
            proposal = entry.get("proposal") or {}
            writer.writerow(
                {
                    "hash": entry.get("hash"),
                    "date": entry.get("date"),
                    "family": entry.get("family"),
                    "currentSubject": entry.get("currentSubject"),
                    "proposedSubject": proposal.get("subject"),
                    "proposedBody": "\n".join(proposal.get("bodyLines") or []),
                    "proposedMessage": proposal.get("message"),
                    "status": proposal.get("status"),
                    "confidence": proposal.get("confidence"),
                    "reason": proposal.get("reason"),
                }
            )
    return {"json": str(json_path), "markdown": str(markdown_path), "csv": str(csv_path)}


def default_output_root(repo_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return repo_root / ".claude-state" / "commit-message-rewrite" / stamp


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a reviewed map for retroactive vague closeout commit messages.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--ref", default="master")
    parser.add_argument("--output-root")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    output_root = Path(args.output_root).resolve() if args.output_root else default_output_root(repo_root)
    plan = build_rewrite_plan(repo_root, args.ref)
    outputs = write_plan(plan, output_root)
    print(json.dumps({"status": "success", "outputs": outputs, "summary": {k: plan[k] for k in ("vagueCommitCount", "familyCounts", "proposalStatusCounts", "rewriteSafety")}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
