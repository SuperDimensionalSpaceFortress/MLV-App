from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .core import HygieneError, canonical_json, normalize_rel, resolve_repo_root, utc_now


SCHEMA = "candidate-acceptance.v1"
SURFACE_SCHEMA = "candidate-acceptance-surface.v1"
FIX_BATCH_SCHEMA = "acceptance-fix-batch.v1"
PROVIDER_SCHEMA = "candidate-acceptance-github-checks.v1"
CHAIN_SCHEMA = "candidate-acceptance-chain.v1"
DEFAULT_REQUIRED_SURFACES = (
    "content-self",
    "content-stranger-1",
    "content-stranger-2",
    "hosted-tests",
    "hosted-codeql",
)
CANONICAL_PROVIDER_REPOSITORY = "layibabalola/MLV-App"
CONTENT_REVIEWERS = {
    "content-self": "root",
    "content-stranger-1": "release_combined_review",
    "content-stranger-2": "release_failopen_review",
}


def target_candidate_acceptance_policy(
    repo_root: Path,
    detection: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Read the candidate-acceptance activation root from the pinned target."""

    target_head = _require_sha(detection.get("targetHead"), "targetHead")
    from .brokered_closeout import git_stdout

    raw = git_stdout(repo_root, ["show", f"{target_head}:closeout.config.json"], required=False)
    if not raw:
        raise HygieneError("pinned target closeout.config.json is unavailable")
    try:
        target_config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HygieneError("pinned target closeout.config.json is malformed") from exc
    if not isinstance(target_config, dict):
        raise HygieneError("pinned target closeout.config.json is malformed")
    policy = target_config.get("candidateAcceptance")
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise HygieneError("pinned target candidateAcceptance policy is malformed")
    return policy


def _activation_state(policy: Dict[str, Any], label: str) -> bool:
    for key in ("enabled", "requireReadyForFinalize"):
        if not isinstance(policy.get(key), bool):
            raise HygieneError(f"{label} candidateAcceptance.{key} must be boolean")
    if policy["enabled"] != policy["requireReadyForFinalize"]:
        raise HygieneError(f"{label} candidateAcceptance activation state is inconsistent")
    return bool(policy["enabled"])


def candidate_acceptance_enforced(
    repo_root: Path,
    config: Dict[str, Any],
    detection: Dict[str, Any],
) -> bool:
    """Return whether this candidate must satisfy acceptance.

    The infrastructure must first land dormant.  A later, separately reviewed
    candidate may activate it against that landed policy.  Once the target is
    active, candidates cannot disable or repoint it.
    """

    candidate = _acceptance_config(config)
    target = target_candidate_acceptance_policy(repo_root, detection)
    candidate_active = _activation_state(candidate, "candidate")
    if target is None:
        if not candidate_active:
            return False
        raise HygieneError("candidate acceptance infrastructure must land dormant before activation")

    target_active = _activation_state(target, "pinned target")
    if target_active:
        if not candidate_active:
            raise HygieneError("candidate cannot disable the pinned target candidateAcceptance barrier")
        if canonical_json(candidate) != canonical_json(target):
            raise HygieneError("candidate candidateAcceptance policy differs from the active pinned target")
        return True

    if not candidate_active:
        if canonical_json(candidate) != canonical_json(target):
            raise HygieneError("dormant candidateAcceptance policy differs from the pinned target")
        return False
    activation_candidate = dict(candidate)
    activation_target = dict(target)
    for policy in (activation_candidate, activation_target):
        policy.pop("enabled", None)
        policy.pop("requireReadyForFinalize", None)
    if canonical_json(activation_candidate) != canonical_json(activation_target):
        raise HygieneError("candidateAcceptance activation may change only the two activation booleans")
    return True


def content_review_gate_trust_error(
    repo_root: Path,
    config: Dict[str, Any],
    detection: Dict[str, Any],
) -> Optional[str]:
    """Bind the human authority gate to the already-landed target policy.

    Candidate acceptance is process evidence, so it cannot authorize weakening the
    separate human gate in the same candidate.  A gate-policy change must land first
    through the prior target policy, then a later candidate may inherit that new root.
    """

    try:
        target_head = _require_sha(detection.get("targetHead"), "targetHead")
    except HygieneError as exc:
        return str(exc)
    from .brokered_closeout import git_stdout

    raw = git_stdout(repo_root, ["show", f"{target_head}:closeout.config.json"], required=False)
    if not raw:
        return "pinned target closeout.config.json is unavailable"
    try:
        target_config = json.loads(raw)
    except json.JSONDecodeError:
        return "pinned target closeout.config.json is malformed"
    target_gate = target_config.get("contentReviewGate") if isinstance(target_config, dict) else None
    candidate_gate = config.get("contentReviewGate") if isinstance(config, dict) else None
    if not isinstance(target_gate, dict) or not isinstance(candidate_gate, dict):
        return "contentReviewGate trust root is missing"
    if not bool(target_gate.get("requireClaudeApprovalForFinalize")) or not bool(target_gate.get("requireHandoff")):
        return "pinned target contentReviewGate is not fail-closed"
    for key in ("coordinationFile", "handoffActor", "handoffKind", "reviewActor", "reviewKind"):
        if not str(target_gate.get(key) or "").strip():
            return f"pinned target contentReviewGate.{key} is empty"
    sessions = target_gate.get("authorizedReviewSessions")
    if not isinstance(sessions, list) or not sessions or any(not str(item).strip() for item in sessions):
        return "pinned target contentReviewGate authorized review sessions are empty"
    if canonical_json(candidate_gate) != canonical_json(target_gate):
        return "candidate contentReviewGate differs from the pinned target trust root"
    return None
TERMINAL_VERDICTS = {"APPROVE", "CHANGES_REQUESTED", "UNAVAILABLE"}
BLOCKING_VERDICTS = {"CHANGES_REQUESTED", "UNAVAILABLE"}
HOSTED_SURFACES = {"hosted-tests", "hosted-codeql"}
HOSTED_CHECKS = {
    "hosted-tests": (
        ("Repo Hygiene Python (windows-latest)", 15368, False),
        ("Repo Hygiene Python (ubuntu-latest)", 15368, False),
        ("Factory Bridge Regressions", 15368, False),
        ("Windows GUI Pilot", 15368, False),
        ("Windows Product Oracles", 15368, False),
        ("Protected Check Route", 15368, False),
    ),
    "hosted-codeql": (
        ("Analyze (actions)", 15368, True),
        ("Analyze (c-cpp)", 15368, True),
        ("Analyze (python)", 15368, True),
        ("CodeQL", 57789, True),
    ),
}


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise HygieneError(f"{label} must be a full lowercase Git SHA")
    return text


def _acceptance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("candidateAcceptance")
    return raw if isinstance(raw, dict) else {}


def acceptance_root(repo_root: Path, config: Dict[str, Any]) -> Path:
    rel = normalize_rel(str(_acceptance_config(config).get("stateRoot") or ".claude-state/closeout/acceptance"))
    if not rel.startswith(".claude-state/"):
        raise HygieneError("candidate acceptance stateRoot must stay under .claude-state/")
    root = (repo_root / rel).resolve()
    state = (repo_root / ".claude-state").resolve()
    if root != state and state not in root.parents:
        raise HygieneError("candidate acceptance stateRoot escapes .claude-state/")
    return root


def required_surfaces(config: Dict[str, Any]) -> List[str]:
    raw: Iterable[Any] = _acceptance_config(config).get("requiredSurfaces") or DEFAULT_REQUIRED_SURFACES
    result = [str(item).strip() for item in raw if str(item).strip()]
    if result != list(DEFAULT_REQUIRED_SURFACES):
        raise HygieneError("candidate acceptance required surfaces must equal the mandatory surface set")
    return result


def provider_repository(config: Dict[str, Any]) -> str:
    repository = str(_acceptance_config(config).get("providerRepository") or "").strip()
    if repository != CANONICAL_PROVIDER_REPOSITORY:
        raise HygieneError("candidate acceptance providerRepository must equal the canonical repository")
    return repository


def validation_plan(config: Dict[str, Any]) -> Dict[str, Any]:
    validation = config.get("validation") if isinstance(config.get("validation"), dict) else {}
    commands = validation.get("commands") if isinstance(validation.get("commands"), list) else []
    return {
        "commands": commands,
        "runFullSuiteByDefault": bool(validation.get("runFullSuiteByDefault", False)),
        "fullSuiteEnvVar": str(validation.get("fullSuiteEnvVar") or "CLOSEOUT_RUN_FULL_VALIDATION"),
        "boundedRunner": config.get("boundedRunner") if isinstance(config.get("boundedRunner"), dict) else {},
    }


def candidate_identity(
    *,
    target_head: str,
    feature_head: str,
    policy_hash: str,
    plan_hash: str,
    rehearsal: Dict[str, Any],
) -> Dict[str, Any]:
    target = _require_sha(target_head, "targetHead")
    feature = _require_sha(feature_head, "featureHead")
    if rehearsal.get("clean") is True:
        _require_sha(rehearsal.get("integrationHead"), "integrationHead")
        _require_sha(rehearsal.get("integrationTree"), "integrationTree")
        diff_sha = str(rehearsal.get("diffSha256") or "").lower()
        if len(diff_sha) != 64 or any(ch not in "0123456789abcdef" for ch in diff_sha):
            raise HygieneError("diffSha256 must be a lowercase SHA-256 for a clean rehearsal")
    identity = {
        "targetHead": target,
        "featureHead": feature,
        "integrationHead": rehearsal.get("integrationHead"),
        "integrationTree": rehearsal.get("integrationTree"),
        "diffSha256": rehearsal.get("diffSha256"),
        "changedPaths": sorted(
            {normalize_rel(str(path)) for path in rehearsal.get("changedPaths", []) if normalize_rel(str(path))}
        ),
        "policyHash": str(policy_hash),
        "validationPlanHash": str(plan_hash),
    }
    stable_identity = {key: value for key, value in identity.items() if key != "integrationHead"}
    identity["acceptanceTupleHash"] = _hash(stable_identity)
    return identity


def surface_record(
    candidate: Dict[str, Any],
    *,
    surface: str,
    verdict: str,
    reviewer: str,
    session_id: str,
    findings: Optional[Sequence[Dict[str, Any]]] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    surface_name = str(surface).strip()
    reviewer_name = str(reviewer).strip()
    session = str(session_id).strip()
    normalized_verdict = str(verdict).upper().strip()
    if not surface_name or not reviewer_name or not session:
        raise HygieneError("surface, reviewer, and sessionId are required")
    if normalized_verdict not in TERMINAL_VERDICTS:
        raise HygieneError("surface verdict must be APPROVE, CHANGES_REQUESTED, or UNAVAILABLE")
    normalized_findings = []
    for item in findings or []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise HygieneError("each surface finding must be an object with a non-empty id")
        normalized_findings.append(
            {
                "id": str(item["id"]),
                "path": normalize_rel(str(item.get("path") or "")),
                "line": item.get("line"),
                "invariant": str(item.get("invariant") or ""),
                "falsifier": str(item.get("falsifier") or ""),
                "detail": str(item.get("detail") or ""),
            }
        )
    record = {
        "schema": SURFACE_SCHEMA,
        "createdAt": created_at or utc_now(),
        "surface": surface_name,
        "verdict": normalized_verdict,
        "reviewer": reviewer_name,
        "sessionId": session,
        "acceptanceTupleHash": candidate["acceptanceTupleHash"],
        "targetHead": candidate["targetHead"],
        "featureHead": candidate["featureHead"],
        "integrationTree": candidate.get("integrationTree"),
        "diffSha256": candidate.get("diffSha256"),
        "validationPlanHash": candidate.get("validationPlanHash"),
        "policyHash": candidate["policyHash"],
        "findings": normalized_findings,
        "authority": {
            "kind": "agent_or_hosted_acceptance_evidence",
            "grantsHumanAuthority": False,
            "mayReblessGolden": False,
            "mayPublishRelease": False,
        },
    }
    record["recordHash"] = _hash(record)
    return record


def _provider_payload(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PROVIDER_SCHEMA:
        raise HygieneError("GitHub provider evidence schema mismatch")
    response = payload.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("check_runs"), list):
        raise HygieneError("GitHub provider evidence response is malformed")
    total = response.get("total_count")
    if not isinstance(total, int) or total < 0 or total > 100 or total != len(response["check_runs"]):
        raise HygieneError("GitHub provider evidence is truncated or exceeds the single-page bound")
    return payload


def _selected_github_checks(payload: Dict[str, Any], surface: str, candidate: Dict[str, Any], repository: str) -> List[Dict[str, Any]]:
    if surface not in HOSTED_SURFACES:
        raise HygieneError("GitHub provider evidence may record only hosted surfaces")
    if payload.get("repository") != repository:
        raise HygieneError("GitHub provider repository does not match the configured repository")
    if payload.get("headSha") != candidate["featureHead"]:
        raise HygieneError("GitHub provider head does not match the candidate feature head")
    runs = payload["response"]["check_runs"]
    for run in runs:
        if not isinstance(run, dict) or run.get("head_sha") != candidate["featureHead"]:
            raise HygieneError("GitHub provider response contains a malformed or different-head check")
    selected: List[Dict[str, Any]] = []
    for name, expected_app, require_annotations in HOSTED_CHECKS[surface]:
        matches = [run for run in runs if run.get("name") == name]
        if not matches:
            raise HygieneError(f"GitHub provider evidence is missing check: {name}")
        matches.sort(key=lambda run: (str(run.get("started_at") or ""), int(run.get("id") or 0)), reverse=True)
        run = matches[0]
        output = run.get("output")
        if not isinstance(output, dict):
            raise HygieneError(f"GitHub check output is missing: {name}")
        if require_annotations and ("annotations_count" not in output or not isinstance(output["annotations_count"], int)):
            raise HygieneError(f"GitHub check annotation count is missing: {name}")
        app = run.get("app")
        app_id = app.get("id") if isinstance(app, dict) else None
        selected.append(
            {
                "name": name,
                "id": run.get("id"),
                "startedAt": run.get("started_at"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "appId": app_id,
                "annotationsCount": output.get("annotations_count"),
                "detailsUrl": run.get("details_url"),
                "expectedAppId": expected_app,
                "requiresZeroAnnotations": require_annotations,
            }
        )
    return selected


def _provider_failures(selected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in selected
        if item["status"] == "completed"
        and (
            item["conclusion"] != "success"
            or item["appId"] != item["expectedAppId"]
            or (item["requiresZeroAnnotations"] and item["annotationsCount"] != 0)
        )
    ]


def _provider_findings(selected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": f"github-check-{item['id']}",
            "path": "",
            "line": None,
            "invariant": f"{item['name']} must pass at the exact candidate head from its expected app",
            "falsifier": (
                f"status={item['status']}; conclusion={item['conclusion']}; app={item['appId']}; "
                f"annotations={item['annotationsCount']}"
            ),
            "detail": str(item.get("detailsUrl") or ""),
        }
        for item in _provider_failures(selected)
    ]


_WINDOWS_CURL_TRUST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
# The stage lines are diagnostic only: they go to stderr, which the verdict never reads, so a stalled
# probe's partial stderr says which statement it was inside.
[Console]::Error.WriteLine('stage start')
$ClientPath = $env:MLVAPP_SYSTEM_CURL_PATH
if ([string]::IsNullOrWhiteSpace($ClientPath)) { throw 'system curl path is missing' }
$moduleRoot = "$PSHOME\Modules"
[Console]::Error.WriteLine('stage import-management')
Microsoft.PowerShell.Core\Import-Module -Name "$moduleRoot\Microsoft.PowerShell.Management\Microsoft.PowerShell.Management.psd1" -Force -ErrorAction Stop
[Console]::Error.WriteLine('stage import-security')
Microsoft.PowerShell.Core\Import-Module -Name "$moduleRoot\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1" -Force -ErrorAction Stop
[Console]::Error.WriteLine('stage import-utility')
Microsoft.PowerShell.Core\Import-Module -Name "$moduleRoot\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1" -Force -ErrorAction Stop
$PSModuleAutoLoadingPreference = 'None'
[Console]::Error.WriteLine('stage identity')
$clientItem = Microsoft.PowerShell.Management\Get-Item -LiteralPath $ClientPath -Force
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
$currentSid = $identity.User.Value
$broadSids = @('S-1-1-0', 'S-1-5-4', 'S-1-5-11', 'S-1-5-32-545')
$trustedOwnerSids = @('S-1-5-18', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
$fileWriteMask = [int64]([System.Security.AccessControl.FileSystemRights]::WriteData -bor
    [System.Security.AccessControl.FileSystemRights]::AppendData -bor
    [System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [System.Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [System.Security.AccessControl.FileSystemRights]::Delete -bor
    [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [System.Security.AccessControl.FileSystemRights]::TakeOwnership)
$directoryReplaceMask = [int64]([System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [System.Security.AccessControl.FileSystemRights]::Delete -bor
    [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [System.Security.AccessControl.FileSystemRights]::TakeOwnership)
[Console]::Error.WriteLine('stage chain')
$chain = [System.Collections.Generic.List[object]]::new()
$cursor = $clientItem.Directory
while ($null -ne $cursor) {
    $chain.Insert(0, $cursor)
    $cursor = $cursor.Parent
}
$chain.Add($clientItem)
$pathTrust = @()
foreach ($pathItem in $chain) {
    if ($pathItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "system curl trust path must not contain a reparse point: $($pathItem.FullName)"
    }
    [Console]::Error.WriteLine('stage get-acl' + ' ' + $pathItem.FullName)
    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $pathItem.FullName
    $ownerSid = ([System.Security.Principal.NTAccount]$acl.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $unsafeWriteGrants = @()
    $rawDescriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
    $daclPresent = ($rawDescriptor.ControlFlags -band [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent) -ne 0
    $daclNull = $null -eq $rawDescriptor.DiscretionaryAcl
    if (-not $daclPresent -or $daclNull) {
        $unsafeWriteGrants += [ordered]@{ sid = 'NULL_DACL'; rights = 'FullControl' }
    } else {
        $effectiveMask = if ($pathItem -is [System.IO.DirectoryInfo]) { $directoryReplaceMask } else { $fileWriteMask }
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
            if ($rule.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
            if (([int64]$rule.FileSystemRights -band $effectiveMask) -eq 0) { continue }
            try {
                $sidObject = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier])
                $sid = $sidObject.Value
            } catch {
                $unsafeWriteGrants += [ordered]@{ sid = [string]$rule.IdentityReference; rights = [string]$rule.FileSystemRights }
                continue
            }
            $currentTokenCanWrite = ($sid -eq $currentSid) -or $principal.IsInRole($sidObject)
            if ($currentTokenCanWrite -or ($broadSids -contains $sid)) {
                $unsafeWriteGrants += [ordered]@{ sid = $sid; rights = [string]$rule.FileSystemRights }
            }
        }
    }
    $pathTrust += [ordered]@{
        path = $pathItem.FullName
        ownerSid = $ownerSid
        ownerTrusted = $trustedOwnerSids -contains $ownerSid
        daclPresent = $daclPresent
        daclNull = $daclNull
        unsafeWriteGrants = @($unsafeWriteGrants)
    }
}
[Console]::Error.WriteLine('stage signature')
$signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $ClientPath
[Console]::Error.WriteLine('stage emit')
[ordered]@{
    modulePath = [string]$env:PSModulePath
    loadedModulePaths = @(Microsoft.PowerShell.Core\Get-Module | Microsoft.PowerShell.Core\ForEach-Object { $_.Path })
    signatureStatus = [string]$signature.Status
    signerSubject = [string]$signature.SignerCertificate.Subject
    signerThumbprint = [string]$signature.SignerCertificate.Thumbprint
    pathTrust = @($pathTrust)
} | Microsoft.PowerShell.Utility\ConvertTo-Json -Depth 8 -Compress
"""


def _minimal_system_child_environment() -> Dict[str, str]:
    """Return a fixed environment with no loader, proxy, config, or runtime injection."""

    if os.name == "nt":
        return {
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "PATH": r"C:\Windows\System32",
            "NoDefaultCurrentDirectoryInExePath": "1",
        }
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }


def _trusted_unix_curl_path_chain(client: Path) -> List[Dict[str, Any]]:
    """Validate every fixed-path component that can replace the Unix curl binary."""

    chain = [*reversed(client.parents), client]
    evidence: List[Dict[str, Any]] = []
    for index, path in enumerate(chain):
        metadata = path.lstat()
        expected_directory = index < len(chain) - 1
        if stat.S_ISLNK(metadata.st_mode):
            raise HygieneError(f"system curl trust path must not contain a symbolic link: {path}")
        if expected_directory and not stat.S_ISDIR(metadata.st_mode):
            raise HygieneError(f"system curl trust ancestor is not a directory: {path}")
        if not expected_directory and not stat.S_ISREG(metadata.st_mode):
            raise HygieneError("system curl must be a regular file")
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise HygieneError(f"system curl trust path must be root-owned and not group/other writable: {path}")
        evidence.append({
            "path": str(path),
            "ownerUid": metadata.st_uid,
            "mode": stat.S_IMODE(metadata.st_mode),
        })
    return evidence


def _system_curl_path() -> Path:
    if os.name == "nt":
        client = Path(r"C:\Windows\System32\curl.exe")
    else:
        client = Path("/usr/bin/curl")
    resolved = client.resolve()
    if not resolved.is_absolute() or not resolved.is_file():
        raise HygieneError("candidate acceptance OS-protected system curl is unavailable")
    if os.name == "nt":
        if str(resolved).casefold() != str(client).casefold():
            raise HygieneError("candidate acceptance system curl path is not canonical")
    elif resolved != Path("/usr/bin/curl"):
        raise HygieneError("candidate acceptance system curl must resolve to /usr/bin/curl")
    return resolved


def _windows_curl_trust_probe(repo_root: Path, config: Dict[str, Any], client: Path) -> Dict[str, Any]:
    """Run the protected-module trust probe and verify its own integrity (process exit, evidence
    shape, pinned module path, modules loaded only from the protected root).

    This is the stage BEFORE the ACL/signature policy: it says the probe ran untampered, not that
    the curl path chain is safe. _trusted_system_curl_identity applies the policy on top of it.
    """

    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.is_file():
        raise HygieneError("Windows system PowerShell is unavailable for curl trust verification")
    from .brokered_closeout import run_bounded_closeout_process

    trust_env = _minimal_system_child_environment()
    trust_env["MLVAPP_SYSTEM_CURL_PATH"] = str(client)
    trust_env["PSModulePath"] = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"
    completed = run_bounded_closeout_process(
        repo_root,
        config,
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _WINDOWS_CURL_TRUST_SCRIPT,
        ],
        timeout_ms=30000,
        max_output_bytes=65536,
        recovery_command="restore the Microsoft-signed OS curl binary and its protected ACL",
        normalize_failure_text=False,
        env=trust_env,
        resource_overrides={"profile": "provider-verification", "affinityCores": 2},
    )
    if completed["returncode"] != 0 or completed.get("timedOut") or completed.get("outputCapped") or completed.get("cpuStalled"):
        raise HygieneError(f"system curl trust verification failed with exit {completed['returncode']}: {completed['stderr'][-1000:]}")
    try:
        trust = json.loads(completed["stdout"])
    except json.JSONDecodeError as exc:
        raise HygieneError("system curl trust evidence is malformed") from exc
    path_trust = trust.get("pathTrust")
    if not isinstance(path_trust, list) or len(path_trust) < 3:
        raise HygieneError("system curl path-chain trust evidence is incomplete")
    expected_module_root = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"
    expected_module_paths = {
        r"C:\Program Files\WindowsPowerShell\Modules".casefold(),
        expected_module_root.casefold(),
    }
    actual_module_paths = {
        value.strip().casefold()
        for value in str(trust.get("modulePath") or "").split(";")
        if value.strip()
    }
    if actual_module_paths != expected_module_paths:
        raise HygieneError("system curl trust verification used an unpinned PowerShell module path")
    loaded_modules = trust.get("loadedModulePaths")
    if not isinstance(loaded_modules, list) or not loaded_modules:
        raise HygieneError("system curl trust verification did not report its loaded modules")
    if any(not str(path).casefold().startswith(expected_module_root.casefold() + "\\") for path in loaded_modules):
        raise HygieneError("system curl trust verification loaded a module outside the protected system root")
    return trust


def _trusted_system_curl_identity(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """Return fail-closed ownership/signature evidence for the OS curl binary."""

    client = _system_curl_path()
    trust: Dict[str, Any]
    if os.name == "nt":
        trust = _windows_curl_trust_probe(repo_root, config, client)
        path_trust = trust["pathTrust"]
        if any(not isinstance(item, dict) or not item.get("ownerTrusted") for item in path_trust):
            raise HygieneError("system curl path chain is not owned by Windows or TrustedInstaller")
        if any(item.get("daclPresent") is not True or item.get("daclNull") is not False for item in path_trust):
            raise HygieneError("system curl path chain has an absent or NULL discretionary ACL")
        if trust.get("signatureStatus") != "Valid" or "O=Microsoft Corporation" not in str(trust.get("signerSubject") or ""):
            raise HygieneError("system curl does not have a valid Microsoft signature")
        unsafe_grants = [
            f"{item.get('path')}: {grant.get('sid')} {grant.get('rights')}"
            for item in path_trust
            for grant in item.get("unsafeWriteGrants") or []
        ]
        if unsafe_grants:
            raise HygieneError(
                "system curl path chain grants replacement authority to the current token or a broad principal: "
                + "; ".join(unsafe_grants)
            )
    else:
        trust = {
            "pathTrust": _trusted_unix_curl_path_chain(client),
        }
    return {
        "kind": "os-protected-system-curl",
        "path": str(client),
        "size": client.stat().st_size,
        "sha256": _file_sha256(client),
        "trust": trust,
    }


def system_curl_github_query_command(
    repository: str,
    head_sha: str,
    max_output_bytes: int,
) -> tuple[Path, List[str]]:
    """Build a config-free query through the exact OS-protected curl binary."""

    provider_client = _system_curl_path()
    if not provider_client.is_absolute() or not provider_client.is_file():
        raise HygieneError("candidate acceptance system curl is unavailable")
    head = _require_sha(head_sha, "featureHead")
    if repository != CANONICAL_PROVIDER_REPOSITORY:
        raise HygieneError("live GitHub query repository must equal the canonical repository")
    url = f"https://api.github.com/repos/{repository}/commits/{head}/check-runs?per_page=100"
    return provider_client, [
        str(provider_client),
        "-q",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--connect-timeout",
        "10",
        "--max-time",
        "20",
        "--max-filesize",
        str(max_output_bytes),
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "User-Agent: MLV-App-candidate-acceptance/1",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        "--write-out",
        "\n%{http_code}",
        url,
    ]


def _live_github_json_payload(
    repo_root: Path,
    config: Dict[str, Any],
    repository: str,
    url: str,
    *,
    recovery_command: str,
) -> Dict[str, Any]:
    """Read one GitHub JSON resource through the OS-protected curl trust root."""

    repo_root = resolve_repo_root(repo_root)
    if repository != CANONICAL_PROVIDER_REPOSITORY:
        raise HygieneError("live GitHub query repository must equal the canonical repository")
    api_prefix = f"https://api.github.com/repos/{repository}/"
    if not isinstance(url, str) or not url.startswith(api_prefix):
        raise HygieneError("live GitHub query URL must stay under the canonical repository API")
    process_env = _minimal_system_child_environment()
    from .brokered_closeout import closeout_max_process_output_bytes, run_bounded_closeout_process

    output_cap = min(closeout_max_process_output_bytes(config), 4 * 1024 * 1024)
    provider_client_identity = _trusted_system_curl_identity(repo_root, config)
    provider_client = _system_curl_path()
    if str(provider_client) != provider_client_identity["path"]:
        raise HygieneError("system curl command path differs from the trusted client identity")
    command = [
        str(provider_client),
        "-q",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--connect-timeout",
        "10",
        "--max-time",
        "20",
        "--max-filesize",
        str(output_cap),
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "User-Agent: MLV-App-candidate-acceptance/1",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        "--write-out",
        "\n%{http_code}",
        url,
    ]
    completed = run_bounded_closeout_process(
        repo_root,
        config,
        command,
        timeout_ms=60000,
        max_output_bytes=output_cap,
        recovery_command=recovery_command,
        normalize_failure_text=False,
        env=process_env,
        resource_overrides={"profile": "provider-verification", "affinityCores": 2},
    )
    if completed["returncode"] != 0 or completed.get("timedOut") or completed.get("outputCapped") or completed.get("cpuStalled"):
        raise HygieneError(f"live GitHub provider verification failed with exit {completed['returncode']}: {completed['stderr'][-1000:]}")
    if _trusted_system_curl_identity(repo_root, config) != provider_client_identity:
        raise HygieneError("candidate acceptance system curl identity changed during live verification")
    body, separator, raw_status = completed["stdout"].rstrip().rpartition("\n")
    if not separator or raw_status.strip() != "200":
        raise HygieneError(f"live GitHub provider returned unexpected HTTP status: {raw_status.strip() or 'missing'}")
    try:
        response = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HygieneError("live GitHub provider response is malformed") from exc
    return {
        "capturedAt": utc_now(),
        "repository": repository,
        "providerClient": provider_client_identity,
        "response": response,
    }


def live_github_issue_comment(
    repo_root: Path,
    config: Dict[str, Any],
    repository: str,
    comment_id: int,
) -> Dict[str, Any]:
    """Fetch one exact repository issue/PR comment from canonical GitHub."""

    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
        raise HygieneError("live GitHub comment id must be a positive integer")
    url = f"https://api.github.com/repos/{repository}/issues/comments/{comment_id}"
    payload = _live_github_json_payload(
        repo_root,
        config,
        repository,
        url,
        recovery_command="restore public api.github.com HTTPS access and rerun payload provenance validation",
    )
    if not isinstance(payload.get("response"), dict):
        raise HygieneError("live GitHub issue comment response is malformed")
    return payload


def live_github_branch_head(
    repo_root: Path,
    config: Dict[str, Any],
    repository: str,
    branch: str,
) -> str:
    """Resolve one exact canonical GitHub branch through the protected provider client."""

    if branch != "master":
        raise HygieneError("live GitHub branch query is restricted to master")
    url = f"https://api.github.com/repos/{repository}/git/ref/heads/{branch}"
    payload = _live_github_json_payload(
        repo_root,
        config,
        repository,
        url,
        recovery_command="restore public api.github.com HTTPS access and rerun payload provenance validation",
    )
    response = payload.get("response")
    target = response.get("object") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("ref") != f"refs/heads/{branch}"
        or not isinstance(target, dict)
        or target.get("type") != "commit"
    ):
        raise HygieneError("live GitHub branch response is malformed")
    return _require_sha(target.get("sha"), "live GitHub master head")


def _live_github_provider_payload(repo_root: Path, config: Dict[str, Any], repository: str, head_sha: str) -> Dict[str, Any]:
    head = _require_sha(head_sha, "featureHead")
    url = f"https://api.github.com/repos/{repository}/commits/{head}/check-runs?per_page=100"
    raw = _live_github_json_payload(
        repo_root,
        config,
        repository,
        url,
        recovery_command="restore public api.github.com HTTPS access and rerun candidate acceptance",
    )
    response = raw["response"]
    payload = {
        "schema": PROVIDER_SCHEMA,
        "capturedAt": raw["capturedAt"],
        "repository": repository,
        "headSha": head,
        "providerClient": raw["providerClient"],
        "response": response,
    }
    if not isinstance(response, dict) or not isinstance(response.get("check_runs"), list):
        raise HygieneError("live GitHub provider response is malformed")
    total = response.get("total_count")
    if not isinstance(total, int) or total < 0 or total > 100 or total != len(response["check_runs"]):
        raise HygieneError("live GitHub provider response is truncated or exceeds the single-page bound")
    return payload


def verify_live_provider(
    repo_root: Path,
    ledger: Dict[str, Any],
    config: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    candidate = ledger.get("candidate") if isinstance(ledger.get("candidate"), dict) else {}
    repository = provider_repository(config)
    head_sha = _require_sha(candidate.get("featureHead"), "featureHead")
    live = payload if payload is not None else _live_github_provider_payload(repo_root, config, repository, head_sha)
    hosted_records = {
        str(record.get("surface")): record
        for record in ledger.get("surfaces") or []
        if isinstance(record, dict) and record.get("surface") in HOSTED_SURFACES
    }
    if set(hosted_records) != HOSTED_SURFACES:
        raise HygieneError("ready candidate acceptance ledger lacks both hosted provider records")
    for surface in sorted(HOSTED_SURFACES):
        selected = _selected_github_checks(live, surface, candidate, repository)
        if any(item.get("status") != "completed" for item in selected):
            raise HygieneError(f"live GitHub provider surface is not terminal: {surface}")
        if _provider_failures(selected):
            raise HygieneError(f"live GitHub provider surface is not successful: {surface}")
        recorded = hosted_records[surface].get("providerEvidence")
        if not isinstance(recorded, dict) or recorded.get("selectedChecks") != selected:
            raise HygieneError(f"live GitHub provider evidence drifted from the accepted record: {surface}")


def provider_surface_record(
    repo_root: Path,
    config: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    surface: str,
    evidence_path: Path,
) -> Dict[str, Any]:
    repository = provider_repository(config)
    evidence_path = evidence_path.resolve()
    provider_root = (acceptance_root(repo_root, config) / "provider").resolve()
    expected_parent = provider_root / str(candidate["featureHead"])
    evidence_hash = _file_sha256(evidence_path)
    if evidence_path.parent != expected_parent or evidence_path.name != f"check-runs-{evidence_hash}.json":
        raise HygieneError("GitHub provider evidence must use its exact feature-head/SHA-addressed path")
    payload = _provider_payload(evidence_path)
    selected = _selected_github_checks(payload, surface, candidate, repository)
    failures = _provider_failures(selected)
    nonterminal = [item for item in selected if item["status"] != "completed"]
    if nonterminal:
        raise HygieneError(f"GitHub provider surface is not terminal: {surface}")
    findings = _provider_findings(selected)
    record = surface_record(
        candidate,
        surface=surface,
        verdict="APPROVE" if not failures else "CHANGES_REQUESTED",
        reviewer=f"github:{repository}",
        session_id="github-checks:" + candidate["featureHead"] + ":" + ",".join(str(item["id"]) for item in selected),
        findings=findings,
    )
    record["schema"] = "candidate-acceptance-provider-surface.v1"
    record["providerEvidence"] = {
        "schema": PROVIDER_SCHEMA,
        "repository": repository,
        "headSha": candidate["featureHead"],
        "path": normalize_rel(str(evidence_path.relative_to(repo_root.resolve()))),
        "sha256": evidence_hash,
        "selectedChecks": selected,
    }
    record.pop("recordHash", None)
    record["recordHash"] = _hash(record)
    return record


def _validate_surface(
    record: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    surface_name = str(record.get("surface") or "")
    expected_schema = "candidate-acceptance-provider-surface.v1" if surface_name in HOSTED_SURFACES else SURFACE_SCHEMA
    if record.get("schema") != expected_schema:
        return "schema_mismatch"
    clean = dict(record)
    supplied_hash = clean.pop("recordHash", None)
    if supplied_hash != _hash(clean):
        return "record_hash_mismatch"
    exact = {
        "acceptanceTupleHash": candidate["acceptanceTupleHash"],
        "targetHead": candidate["targetHead"],
        "featureHead": candidate["featureHead"],
        "integrationTree": candidate.get("integrationTree"),
        "diffSha256": candidate.get("diffSha256"),
        "validationPlanHash": candidate.get("validationPlanHash"),
        "policyHash": candidate["policyHash"],
    }
    if any(record.get(key) != value for key, value in exact.items()):
        return "candidate_tuple_mismatch"
    authority = record.get("authority") if isinstance(record.get("authority"), dict) else {}
    expected_authority = {
        "kind": "agent_or_hosted_acceptance_evidence",
        "grantsHumanAuthority": False,
        "mayReblessGolden": False,
        "mayPublishRelease": False,
    }
    if authority != expected_authority:
        return "human_authority_escalation"
    if record.get("verdict") not in TERMINAL_VERDICTS:
        return "nonterminal_verdict"
    if surface_name in HOSTED_SURFACES:
        evidence = record.get("providerEvidence")
        if not isinstance(evidence, dict) or evidence.get("schema") != PROVIDER_SCHEMA:
            return "provider_evidence_missing"
        if repo_root is None or config is None:
            return "provider_evidence_not_revalidated"
        try:
            evidence_path = (repo_root / normalize_rel(str(evidence.get("path") or ""))).resolve()
            provider_root = (acceptance_root(repo_root, config) / "provider").resolve()
            evidence_hash = _file_sha256(evidence_path)
            expected_parent = provider_root / str(candidate["featureHead"])
            if evidence_path.parent != expected_parent or evidence_path.name != f"check-runs-{evidence_hash}.json":
                return "provider_evidence_path_or_name_mismatch"
            if evidence_hash != evidence.get("sha256"):
                return "provider_evidence_hash_mismatch"
            payload = _provider_payload(evidence_path)
            repository = provider_repository(config)
            selected = _selected_github_checks(payload, surface_name, candidate, repository)
        except (HygieneError, OSError, json.JSONDecodeError, ValueError):
            return "provider_evidence_invalid"
        if evidence.get("repository") != repository or evidence.get("headSha") != candidate["featureHead"]:
            return "provider_evidence_identity_mismatch"
        if evidence.get("selectedChecks") != selected:
            return "provider_selected_checks_mismatch"
        if any(item.get("status") != "completed" for item in selected):
            return "provider_checks_nonterminal"
        expected_verdict = "CHANGES_REQUESTED" if _provider_failures(selected) else "APPROVE"
        expected_reviewer = f"github:{repository}"
        expected_session = "github-checks:" + candidate["featureHead"] + ":" + ",".join(
            str(item["id"]) for item in selected
        )
        if record.get("verdict") != expected_verdict:
            return "provider_verdict_mismatch"
        if record.get("reviewer") != expected_reviewer or record.get("sessionId") != expected_session:
            return "provider_identity_mismatch"
        if record.get("findings") != _provider_findings(selected):
            return "provider_findings_mismatch"
    elif surface_name in CONTENT_REVIEWERS and record.get("reviewer") != CONTENT_REVIEWERS[surface_name]:
        return "content_reviewer_identity_mismatch"
    return None


def evaluate(
    *,
    candidate: Dict[str, Any],
    rehearsal: Dict[str, Any],
    required: Sequence[str],
    records: Sequence[Dict[str, Any]],
    repo_root: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_names = list(required)
    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in required_names}
    stale: List[Dict[str, Any]] = []
    for record in records:
        reason = _validate_surface(record, candidate, repo_root=repo_root, config=config)
        surface_name = str(record.get("surface") or "")
        if reason or surface_name not in required_names:
            stale.append({"surface": surface_name, "reason": reason or "surface_not_required", "recordHash": record.get("recordHash")})
            continue
        grouped[surface_name].append(record)

    selected: Dict[str, List[Dict[str, Any]]] = {}
    for surface_name, surface_records in grouped.items():
        if not surface_records:
            continue
        blocking = [record for record in surface_records if record.get("verdict") in BLOCKING_VERDICTS]
        if blocking:
            selected[surface_name] = sorted(blocking, key=lambda record: (str(record.get("createdAt")), str(record.get("recordHash"))))
        else:
            selected[surface_name] = [max(surface_records, key=lambda record: (str(record.get("createdAt")), str(record.get("recordHash"))))]

    missing = [surface for surface in required_names if surface not in selected]
    reviewer_identities: Dict[str, str] = {}
    session_identities: Dict[str, str] = {}
    duplicate_identities: List[Dict[str, str]] = []
    for surface_name, surface_records in selected.items():
        record = surface_records[-1]
        session = str(record.get("sessionId"))
        previous_session = session_identities.get(session)
        if previous_session is not None:
            duplicate_identities.append({"surface": surface_name, "duplicates": previous_session, "identity": "session"})
        else:
            session_identities[session] = surface_name
        if surface_name.startswith("content-"):
            reviewer = str(record.get("reviewer"))
            previous_reviewer = reviewer_identities.get(reviewer)
            if previous_reviewer is not None:
                duplicate_identities.append({"surface": surface_name, "duplicates": previous_reviewer, "identity": "reviewer"})
            else:
                reviewer_identities[reviewer] = surface_name

    blockers: List[Dict[str, Any]] = []
    if rehearsal.get("clean") is not True:
        blockers.append(
            {
                "id": "integration-rehearsal-failed",
                "surface": "integration-rehearsal",
                "reason": str(rehearsal.get("reason") or "unknown rehearsal failure"),
                "detail": rehearsal.get("stderr") or rehearsal.get("stdout") or "",
            }
        )
    for duplicate in duplicate_identities:
        blockers.append(
            {
                "id": f"duplicate-review-{duplicate['identity']}",
                "surface": duplicate["surface"],
                "reason": f"duplicates {duplicate['duplicates']}",
            }
        )
    for surface_name in required_names:
        for record in selected.get(surface_name, []):
            if record.get("verdict") in BLOCKING_VERDICTS:
                findings = record.get("findings") if isinstance(record.get("findings"), list) else []
                if findings:
                    blockers.extend({**finding, "surface": surface_name} for finding in findings)
                else:
                    blockers.append({"id": f"{surface_name}-{str(record.get('verdict')).lower()}", "surface": surface_name, "reason": record.get("verdict")})

    if missing:
        state = "collecting"
    elif blockers:
        state = "changes_required"
    else:
        state = "ready"
    selected_records = [record for name in required_names for record in selected.get(name, [])]
    selected_hashes = [record["recordHash"] for record in selected_records]
    evidence_hash = _hash(
        {
            "acceptanceTupleHash": candidate["acceptanceTupleHash"],
            "rehearsal": rehearsal,
            "surfaceRecordHashes": selected_hashes,
            "missingSurfaces": missing,
            "blockers": blockers,
        }
    )
    result = {
        "schema": SCHEMA,
        "capturedAt": utc_now(),
        "state": state,
        "candidate": candidate,
        "rehearsal": rehearsal,
        "requiredSurfaces": required_names,
        "surfaces": selected_records,
        "missingSurfaces": missing,
        "staleSurfaceRecords": stale,
        "blockers": blockers,
        "authorityBoundaries": {
            "agentApprovalsGrantHumanAuthority": False,
            "humanOnlyActions": ["golden_rebless", "release_publication", "payload_redistribution", "provider_activation"],
            "providerActivation": "CLOSED",
        },
        "evidenceHash": evidence_hash,
    }
    if state == "changes_required":
        result["fixBatch"] = {
            "schema": FIX_BATCH_SCHEMA,
            "acceptanceTupleHash": candidate["acceptanceTupleHash"],
            "evidenceHash": evidence_hash,
            "findingCount": len(blockers),
            "findings": blockers,
        }
    ledger_basis = {key: value for key, value in result.items() if key not in {"capturedAt", "ledgerHash"}}
    result["ledgerHash"] = _hash(ledger_basis)
    return result


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _chain_path(repo_root: Path, config: Dict[str, Any]) -> Path:
    return acceptance_root(repo_root, config) / "ledger-chain.json"


def _chain_error(payload: Dict[str, Any]) -> Optional[str]:
    if payload.get("schema") != CHAIN_SCHEMA or not isinstance(payload.get("tuples"), dict):
        return "candidate acceptance chain schema mismatch"
    clean = dict(payload)
    supplied = clean.pop("chainHash", None)
    if supplied != _hash(clean):
        return "candidate acceptance chain hash mismatch"
    for tuple_hash, hashes in payload["tuples"].items():
        if len(str(tuple_hash)) != 64 or not isinstance(hashes, list) or not hashes or len(hashes) != len(set(hashes)):
            return "candidate acceptance chain tuple history is malformed"
        if any(len(str(item)) != 64 for item in hashes):
            return "candidate acceptance chain ledger hash is malformed"
    return None


def _load_chain(repo_root: Path, config: Dict[str, Any], *, allow_initialize: bool) -> Dict[str, Any]:
    path = _chain_path(repo_root, config)
    if not path.exists():
        root = acceptance_root(repo_root, config)
        prior_state = (root / "latest.json").exists() or (root / "history").exists()
        if prior_state or not allow_initialize:
            raise HygieneError("candidate acceptance monotonic chain is missing")
        chain = {"schema": CHAIN_SCHEMA, "tuples": {}}
        chain["chainHash"] = _hash(chain)
        return chain
    try:
        chain = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HygieneError(f"candidate acceptance monotonic chain is unreadable: {exc}") from exc
    if not isinstance(chain, dict) or _chain_error(chain):
        raise HygieneError(_chain_error(chain) if isinstance(chain, dict) else "candidate acceptance chain is malformed")
    return chain


def _load_chained_ledgers(
    repo_root: Path,
    config: Dict[str, Any],
    chain: Dict[str, Any],
    tuple_hash: str,
) -> List[Dict[str, Any]]:
    history_root = acceptance_root(repo_root, config) / "history"
    ledgers: List[Dict[str, Any]] = []
    for ledger_hash in chain["tuples"].get(tuple_hash, []):
        path = history_root / f"{tuple_hash}-{ledger_hash}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HygieneError(f"candidate acceptance monotonic chain history is unreadable: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise HygieneError(f"candidate acceptance monotonic chain history is malformed: {path}")
        ledger_error = _ledger_error(payload, repo_root=repo_root, config=config)
        if ledger_error:
            raise HygieneError(f"candidate acceptance monotonic chain history is invalid: {path}: {ledger_error}")
        if payload.get("ledgerHash") != ledger_hash:
            raise HygieneError(f"candidate acceptance monotonic chain history hash does not match its index: {path}")
        ledgers.append(payload)
    return ledgers


def _same_tuple_blocker_error(ledgers: Sequence[Dict[str, Any]], proposed_hash: str) -> Optional[str]:
    blocking_hashes = {
        str(item.get("ledgerHash"))
        for item in ledgers
        if item.get("state") == "changes_required" or bool(item.get("blockers"))
    }
    if blocking_hashes and proposed_hash not in blocking_hashes:
        return "candidate acceptance same-tuple blocker is monotonic; a repair requires a new candidate tuple"
    return None


def write_ledger(repo_root: Path, config: Dict[str, Any], ledger: Dict[str, Any]) -> Dict[str, str]:
    repo_root = repo_root.resolve()
    ledger_error = _ledger_error(ledger, repo_root=repo_root, config=config)
    if ledger_error:
        raise HygieneError(f"candidate acceptance ledger is incoherent: {ledger_error}")
    root = acceptance_root(repo_root, config)
    chain = _load_chain(repo_root, config, allow_initialize=True)
    tuple_hash = str(ledger["candidate"]["acceptanceTupleHash"])
    chained_ledgers = _load_chained_ledgers(repo_root, config, chain, tuple_hash)
    blocker_error = _same_tuple_blocker_error(chained_ledgers, str(ledger["ledgerHash"]))
    if blocker_error:
        raise HygieneError(blocker_error)
    history = root / "history" / f"{ledger['candidate']['acceptanceTupleHash']}-{ledger['ledgerHash']}.json"
    latest = root / "latest.json"
    if history.exists():
        existing = json.loads(history.read_text(encoding="utf-8"))
        existing_basis = {key: value for key, value in existing.items() if key not in {"capturedAt", "outputPaths"}}
        ledger_basis = {key: value for key, value in ledger.items() if key not in {"capturedAt", "outputPaths"}}
        if _ledger_error(existing, repo_root=repo_root, config=config) or existing_basis != ledger_basis:
            raise HygieneError("candidate acceptance history path collision")
    else:
        _atomic_json(history, ledger)
    tuple_chain = chain["tuples"].setdefault(tuple_hash, [])
    if ledger["ledgerHash"] not in tuple_chain:
        tuple_chain.append(ledger["ledgerHash"])
        chain.pop("chainHash", None)
        chain["chainHash"] = _hash(chain)
        _atomic_json(_chain_path(repo_root, config), chain)
    elif tuple_chain[-1] != ledger["ledgerHash"]:
        raise HygieneError("candidate acceptance cannot replay an older same-tuple ledger as latest")
    _atomic_json(latest, ledger)
    return {
        "latest": normalize_rel(str(latest.relative_to(repo_root))),
        "history": normalize_rel(str(history.relative_to(repo_root))),
    }


def latest_summary(repo_root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    path = acceptance_root(repo_root, config) / "latest.json"
    rel_path = normalize_rel(str(path.relative_to(repo_root.resolve())))
    if not path.exists():
        return {"schema": SCHEMA, "state": "missing", "path": rel_path, "ready": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": str(exc)}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": "schema mismatch"}
    ledger_error = _ledger_error(payload, repo_root=repo_root, config=config)
    if ledger_error:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": ledger_error}
    try:
        chain = _load_chain(repo_root, config, allow_initialize=False)
    except HygieneError as exc:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": str(exc)}
    tuple_chain = chain["tuples"].get(str(payload["candidate"]["acceptanceTupleHash"]))
    if not isinstance(tuple_chain, list) or not tuple_chain or tuple_chain[-1] != payload.get("ledgerHash"):
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": "latest ledger is not the monotonic chain tip"}
    try:
        chained_ledgers = _load_chained_ledgers(
            repo_root,
            config,
            chain,
            str(payload["candidate"]["acceptanceTupleHash"]),
        )
    except HygieneError as exc:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": str(exc)}
    blocker_error = _same_tuple_blocker_error(chained_ledgers, str(payload.get("ledgerHash")))
    if blocker_error:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": blocker_error}
    history = acceptance_root(repo_root, config) / "history" / (
        f"{payload['candidate']['acceptanceTupleHash']}-{payload['ledgerHash']}.json"
    )
    try:
        history_payload = json.loads(history.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": f"immutable history missing or invalid: {exc}"}
    history_basis = {key: value for key, value in history_payload.items() if key not in {"capturedAt", "outputPaths"}}
    latest_basis = {key: value for key, value in payload.items() if key not in {"capturedAt", "outputPaths"}}
    if history_basis != latest_basis:
        return {"schema": SCHEMA, "state": "invalid", "path": rel_path, "ready": False, "error": "latest ledger does not match immutable history"}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    fix_batch = payload.get("fixBatch") if isinstance(payload.get("fixBatch"), dict) else {}
    return {
        "schema": SCHEMA,
        "state": str(payload.get("state") or "invalid"),
        "ready": payload.get("state") == "ready",
        "path": rel_path,
        "capturedAt": payload.get("capturedAt"),
        "acceptanceTupleHash": candidate.get("acceptanceTupleHash"),
        "targetHead": candidate.get("targetHead"),
        "featureHead": candidate.get("featureHead"),
        "integrationTree": candidate.get("integrationTree"),
        "diffSha256": candidate.get("diffSha256"),
        "validationPlanHash": candidate.get("validationPlanHash"),
        "policyHash": candidate.get("policyHash"),
        "evidenceHash": payload.get("evidenceHash"),
        "ledgerHash": payload.get("ledgerHash"),
        "missingSurfaces": list(payload.get("missingSurfaces") or []),
        "blockerCount": len(payload.get("blockers") or []),
        "fixBatchFindingCount": int(fix_batch.get("findingCount") or 0),
        "authorityBoundaries": payload.get("authorityBoundaries") or {},
    }


def _ledger_error(
    payload: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    if payload.get("schema") != SCHEMA:
        return "schema mismatch"
    basis = {key: value for key, value in payload.items() if key not in {"capturedAt", "ledgerHash", "outputPaths"}}
    if payload.get("ledgerHash") != _hash(basis):
        return "ledger hash mismatch"
    candidate = payload.get("candidate")
    rehearsal = payload.get("rehearsal")
    required = payload.get("requiredSurfaces")
    surfaces = payload.get("surfaces")
    if not isinstance(candidate, dict) or not isinstance(rehearsal, dict):
        return "candidate or rehearsal missing"
    if not isinstance(required, list) or not isinstance(surfaces, list):
        return "required surfaces or surface records missing"
    if config is not None and required != required_surfaces(config):
        return "required surface policy mismatch"
    try:
        recomputed_candidate = candidate_identity(
            target_head=str(candidate.get("targetHead") or ""),
            feature_head=str(candidate.get("featureHead") or ""),
            policy_hash=str(candidate.get("policyHash") or ""),
            plan_hash=str(candidate.get("validationPlanHash") or ""),
            rehearsal=rehearsal,
        )
    except HygieneError as exc:
        return f"candidate identity invalid: {exc}"
    if candidate != recomputed_candidate:
        return "candidate identity mismatch"
    recomputed = evaluate(
        candidate=candidate,
        rehearsal=rehearsal,
        required=required,
        records=surfaces,
        repo_root=repo_root,
        config=config,
    )
    for key in (
        "state",
        "requiredSurfaces",
        "surfaces",
        "missingSurfaces",
        "blockers",
        "authorityBoundaries",
        "evidenceHash",
        "fixBatch",
    ):
        if payload.get(key) != recomputed.get(key):
            return f"ledger coherence mismatch: {key}"
    return None


def validate_for_finalize(repo_root: Path, config: Dict[str, Any], detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        enforced = candidate_acceptance_enforced(repo_root, config, detection)
        if not enforced:
            human_gate_error = content_review_gate_trust_error(repo_root, config, detection)
            if human_gate_error:
                return {
                    "status": "blocked",
                    "reason": "candidate_acceptance_human_gate_trust_drift",
                    "detail": human_gate_error,
                    "recoveryCommand": "restore the pinned target contentReviewGate or land a separately authorized policy change first",
                }
            return None
        configured_surfaces = required_surfaces(config)
        provider_repository(config)
    except HygieneError as exc:
        return {
            "status": "blocked",
            "reason": "candidate_acceptance_policy_weakened",
            "detail": str(exc),
            "recoveryCommand": "restore the mandatory candidateAcceptance policy and rerun acceptance",
        }
    summary = latest_summary(repo_root, config)
    expected = {
        "targetHead": detection.get("targetHead"),
        "featureHead": detection.get("featureHead"),
        "policyHash": config.get("policyHash"),
    }
    mismatches = {
        key: {"expected": value, "actual": summary.get(key)}
        for key, value in expected.items()
        if summary.get(key) != value
    }
    if summary.get("state") == "ready" and not mismatches:
        human_gate_error = content_review_gate_trust_error(repo_root, config, detection)
        if human_gate_error:
            return {
                "status": "blocked",
                "reason": "candidate_acceptance_human_gate_trust_drift",
                "candidateAcceptance": summary,
                "detail": human_gate_error,
                "recoveryCommand": "restore the pinned target contentReviewGate or land a separately authorized policy change first",
            }
        latest_path = acceptance_root(repo_root, config) / "latest.json"
        try:
            ledger = json.loads(latest_path.read_text(encoding="utf-8"))
            verify_live_provider(repo_root, ledger, config)
        except (HygieneError, OSError, json.JSONDecodeError) as exc:
            mismatches["liveProvider"] = {"expected": "exact terminal provider checks", "actual": str(exc)}
        if mismatches:
            return {
                "status": "blocked",
                "reason": "candidate_acceptance_not_ready",
                "candidateAcceptance": summary,
                "mismatches": mismatches,
                "recoveryCommand": "capture exact-head GitHub acceptance evidence and rerun acceptance",
            }
        from .brokered_closeout import simulate_clean_integration

        rehearsal = simulate_clean_integration(
            repo_root,
            config,
            target_head=str(detection.get("targetHead")),
            branch_head=str(detection.get("featureHead")),
            branch_name=str(detection.get("branch") or "candidate-acceptance-finalize"),
            target_branch=str(detection.get("targetBranch") or "master"),
        )
        if rehearsal.get("clean") is True:
            current_candidate = candidate_identity(
                target_head=str(detection.get("targetHead")),
                feature_head=str(detection.get("featureHead")),
                policy_hash=str(config.get("policyHash") or ""),
                plan_hash=_hash(validation_plan(config)),
                rehearsal=rehearsal,
            )
            tuple_mismatches = {
                key: {"expected": current_candidate.get(key), "actual": summary.get(key)}
                for key in ("acceptanceTupleHash", "integrationTree", "diffSha256", "validationPlanHash")
                if summary.get(key) != current_candidate.get(key)
            }
            if not tuple_mismatches:
                return None
            mismatches.update(tuple_mismatches)
        else:
            mismatches["integrationRehearsal"] = {"expected": "clean", "actual": rehearsal.get("reason")}
    return {
        "status": "blocked",
        "reason": "candidate_acceptance_not_ready",
        "candidateAcceptance": summary,
        "mismatches": mismatches,
        "recoveryCommand": (
            "py -3 -m tools.repo_hygiene.candidate_acceptance evaluate --repo-root . "
            f"--target-head {detection.get('targetHead')} --feature-head {detection.get('featureHead')} --write"
        ),
    }


def final_integration_mismatches(accepted: Dict[str, Any], final_range: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: {"expected": accepted.get(key), "actual": final_range.get(key)}
        for key in ("integrationTree", "diffSha256")
        if accepted.get(key) != final_range.get(key)
    }


def _load_records(paths: Sequence[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for raw in paths:
        payload = json.loads(Path(raw).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise HygieneError(f"surface record must be a JSON object: {raw}")
        records.append(payload)
    return records


def _surface_record_paths(repo_root: Path, config: Dict[str, Any], tuple_hash: str) -> List[str]:
    root = acceptance_root(repo_root, config) / "surfaces" / tuple_hash
    return [str(path) for path in sorted(root.glob("*.json"))] if root.exists() else []


def _historical_surface_records(repo_root: Path, config: Dict[str, Any], tuple_hash: str) -> List[Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    chain_path = _chain_path(repo_root, config)
    history_root = acceptance_root(repo_root, config) / "history"
    if not history_root.exists() and not chain_path.exists():
        return []
    chain = _load_chain(repo_root, config, allow_initialize=False)
    for payload in _load_chained_ledgers(repo_root, config, chain, tuple_hash):
        for record in payload.get("surfaces") or []:
            if isinstance(record, dict) and record.get("recordHash"):
                records[str(record["recordHash"])] = record
    return list(records.values())


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from .brokered_closeout import load_closeout_config, simulate_clean_integration

    repo_root = resolve_repo_root(Path(args.repo_root))
    config = load_closeout_config(repo_root)
    target_head = _require_sha(args.target_head, "targetHead")
    feature_head = _require_sha(args.feature_head, "featureHead")
    plan = validation_plan(config)
    rehearsal = simulate_clean_integration(
        repo_root,
        config,
        target_head=target_head,
        branch_head=feature_head,
        branch_name=args.branch_name or "candidate-acceptance-probe",
        target_branch=args.target_branch or "master",
    )
    candidate = candidate_identity(
        target_head=target_head,
        feature_head=feature_head,
        policy_hash=str(config.get("policyHash") or ""),
        plan_hash=_hash(plan),
        rehearsal=rehearsal,
    )
    record_paths = list(args.surface) + _surface_record_paths(repo_root, config, candidate["acceptanceTupleHash"])
    records = _load_records(record_paths)
    known_hashes = {str(record.get("recordHash")) for record in records}
    for historical in _historical_surface_records(repo_root, config, candidate["acceptanceTupleHash"]):
        if str(historical.get("recordHash")) not in known_hashes:
            records.append(historical)
    ledger = evaluate(
        candidate=candidate,
        rehearsal=rehearsal,
        required=required_surfaces(config),
        records=records,
        repo_root=repo_root,
        config=config,
    )
    if args.write:
        ledger["outputPaths"] = write_ledger(repo_root, config, ledger)
    print(json.dumps(ledger, indent=2, sort_keys=True))
    return 0 if ledger["state"] == "ready" else 2


def _cmd_status(args: argparse.Namespace) -> int:
    from .brokered_closeout import load_closeout_config

    repo_root = resolve_repo_root(Path(args.repo_root))
    config = load_closeout_config(repo_root)
    path = Path(args.ledger) if args.ledger else acceptance_root(repo_root, config) / "latest.json"
    if not path.is_absolute():
        path = repo_root / path
    if not path.exists():
        print(json.dumps({"schema": SCHEMA, "state": "missing", "path": str(path)}, indent=2))
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HygieneError("candidate acceptance ledger must be a JSON object")
    ledger_error = _ledger_error(payload, repo_root=repo_root, config=config)
    if ledger_error:
        raise HygieneError(ledger_error)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("state") == "ready" else 2


def _cmd_record(args: argparse.Namespace) -> int:
    from .brokered_closeout import load_closeout_config

    repo_root = resolve_repo_root(Path(args.repo_root))
    config = load_closeout_config(repo_root)
    ledger_path = Path(args.ledger) if args.ledger else acceptance_root(repo_root, config) / "latest.json"
    if not ledger_path.is_absolute():
        ledger_path = repo_root / ledger_path
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(ledger, dict):
        raise HygieneError("candidate acceptance ledger must be a JSON object")
    ledger_error = _ledger_error(ledger, repo_root=repo_root, config=config)
    if ledger_error:
        raise HygieneError(ledger_error)
    candidate = ledger.get("candidate") if isinstance(ledger.get("candidate"), dict) else None
    if not candidate or not candidate.get("acceptanceTupleHash"):
        raise HygieneError("candidate acceptance ledger lacks candidate identity")
    if args.surface in HOSTED_SURFACES:
        raise HygieneError("hosted surfaces require record-hosted with content-addressed provider evidence")
    expected_reviewer = CONTENT_REVIEWERS.get(args.surface)
    if expected_reviewer is None:
        raise HygieneError("record accepts only mandatory content review surfaces")
    if args.reviewer != expected_reviewer:
        raise HygieneError(f"{args.surface} must be recorded by reviewer {expected_reviewer}")
    findings = []
    for raw in args.finding:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise HygieneError("--finding values must be JSON objects")
        findings.append(parsed)
    record = surface_record(
        candidate,
        surface=args.surface,
        verdict=args.verdict,
        reviewer=args.reviewer,
        session_id=args.session_id,
        findings=findings,
    )
    safe_surface = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in args.surface).strip("-")
    if not safe_surface:
        raise HygieneError("surface name does not contain a safe filename component")
    path = acceptance_root(repo_root, config) / "surfaces" / candidate["acceptanceTupleHash"] / f"{safe_surface}-{record['recordHash'][:12]}.json"
    _atomic_json(path, record)
    print(json.dumps({"schema": SURFACE_SCHEMA, "status": "recorded", "path": normalize_rel(str(path.relative_to(repo_root))), "record": record}, indent=2, sort_keys=True))
    return 0


def _cmd_record_hosted(args: argparse.Namespace) -> int:
    from .brokered_closeout import load_closeout_config

    repo_root = resolve_repo_root(Path(args.repo_root))
    config = load_closeout_config(repo_root)
    ledger_path = Path(args.ledger) if args.ledger else acceptance_root(repo_root, config) / "latest.json"
    if not ledger_path.is_absolute():
        ledger_path = repo_root / ledger_path
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(ledger, dict):
        raise HygieneError("candidate acceptance ledger must be a JSON object")
    ledger_error = _ledger_error(ledger, repo_root=repo_root, config=config)
    if ledger_error:
        raise HygieneError(ledger_error)
    candidate = ledger["candidate"]
    record = provider_surface_record(
        repo_root,
        config,
        candidate,
        surface=args.surface,
        evidence_path=(repo_root / args.evidence) if not Path(args.evidence).is_absolute() else Path(args.evidence),
    )
    safe_surface = args.surface.replace("/", "-")
    path = acceptance_root(repo_root, config) / "surfaces" / candidate["acceptanceTupleHash"] / f"{safe_surface}-{record['recordHash'][:12]}.json"
    _atomic_json(path, record)
    print(
        json.dumps(
            {
                "schema": record["schema"],
                "status": "recorded",
                "path": normalize_rel(str(path.relative_to(repo_root))),
                "record": record,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rehearse and batch exact-candidate acceptance findings.")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--repo-root", default=".")
    evaluate_parser.add_argument("--target-head", required=True)
    evaluate_parser.add_argument("--feature-head", required=True)
    evaluate_parser.add_argument("--target-branch", default="master")
    evaluate_parser.add_argument("--branch-name")
    evaluate_parser.add_argument("--surface", action="append", default=[])
    evaluate_parser.add_argument("--write", action="store_true")
    evaluate_parser.set_defaults(func=_cmd_evaluate)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--repo-root", default=".")
    status_parser.add_argument("--ledger")
    status_parser.set_defaults(func=_cmd_status)
    record_parser = sub.add_parser("record")
    record_parser.add_argument("--repo-root", default=".")
    record_parser.add_argument("--ledger")
    record_parser.add_argument("--surface", required=True)
    record_parser.add_argument("--verdict", required=True, choices=sorted(TERMINAL_VERDICTS))
    record_parser.add_argument("--reviewer", required=True)
    record_parser.add_argument("--session-id", required=True)
    record_parser.add_argument("--finding", action="append", default=[])
    record_parser.set_defaults(func=_cmd_record)
    provider_parser = sub.add_parser("record-hosted")
    provider_parser.add_argument("--repo-root", default=".")
    provider_parser.add_argument("--ledger")
    provider_parser.add_argument("--surface", required=True, choices=sorted(HOSTED_SURFACES))
    provider_parser.add_argument("--evidence", required=True)
    provider_parser.set_defaults(func=_cmd_record_hosted)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (HygieneError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": SCHEMA, "state": "invalid", "error": str(exc)}, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
