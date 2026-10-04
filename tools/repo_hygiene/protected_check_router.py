"""Fail-closed routing for branch-protected product checks.

The router never grants product-test credit.  It either requires the real
product/GUI job or records an exact Git-bound N/A decision for a narrowly
allowlisted non-product diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


SCHEMA = "mlv-protected-check-route/v1"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# These surfaces cannot change rendered product behavior.  Anything not on
# this list routes conservatively to the real product and GUI jobs.
NON_PRODUCT_PREFIXES = (
    ".github/requirements/provider-control",
    ".github/workflows/provider-control-candidate.",
    "tools/provider_control/",
)

# The protected-check implementation itself must exercise every real lane.
ROUTER_CONTROL_PATHS = {
    ".github/workflows/tests.yml",
    "tools/repo_hygiene/protected_check_router.py",
    "tools/repo_hygiene/test_repo_hygiene.py",
}


class RouteError(RuntimeError):
    """A Git or receipt condition was not exact enough to route safely."""


@dataclass(frozen=True)
class Route:
    product: bool
    gui: bool
    reason: str


def _git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        stdout = result.stdout if text else result.stdout.decode("utf-8", "replace")
        raise RouteError((stderr or stdout or "git command failed").strip())
    return result.stdout


def _canonical_path(raw: str) -> str:
    path = raw.replace("\\", "/").strip("/")
    if not path or "\x00" in path:
        raise RouteError("changed path is empty or contains NUL")
    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise RouteError(f"changed path is not canonical: {raw!r}")
    return "/".join(parts)


def classify_paths(paths: Iterable[str]) -> Route:
    canonical = tuple(sorted({_canonical_path(path) for path in paths}))
    if not canonical:
        raise RouteError("empty diff cannot receive N/A credit")
    if any(path in ROUTER_CONTROL_PATHS for path in canonical):
        return Route(True, True, "ROUTER_CONTROL_CHANGED_RUN_REAL_ORACLES")
    if all(path.startswith(NON_PRODUCT_PREFIXES) for path in canonical):
        return Route(False, False, "EXPLICIT_NA_PROVIDER_CONTROL_ONLY")
    return Route(True, True, "UNKNOWN_OR_PRODUCT_PATH_RUN_REAL_ORACLES")


def _resolve_commit(repo: Path, revision: str) -> str:
    if not revision:
        raise RouteError("missing revision")
    resolved = str(_git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}" )).strip()
    if not FULL_SHA.fullmatch(resolved):
        raise RouteError(f"revision did not resolve to a full commit: {revision!r}")
    return resolved


def build_receipt(
    repo: Path,
    base: str,
    head: str,
    *,
    force_real: bool = False,
    merge_group: bool = False,
) -> dict:
    repo = repo.resolve()
    if merge_group:
        # A merge-queue entry is the last gate before master: it always runs the real
        # jobs and needs an exact base, never the HEAD^ fallback `main` applies.
        force_real = True
        if not base:
            raise RouteError("merge_group route requires the exact merge-group base sha")
    base_sha = _resolve_commit(repo, base)
    head_sha = _resolve_commit(repo, head)
    if base_sha == head_sha:
        raise RouteError("base and head must differ")
    tree = str(_git(repo, "rev-parse", f"{head_sha}^{{tree}}" )).strip()
    merge_base = str(_git(repo, "merge-base", base_sha, head_sha)).strip()
    if not FULL_SHA.fullmatch(merge_base):
        raise RouteError("base and head have no exact merge base")

    raw_paths = _git(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        merge_base,
        head_sha,
        text=False,
    )
    assert isinstance(raw_paths, bytes)
    paths = [_canonical_path(item.decode("utf-8", "strict")) for item in raw_paths.split(b"\0") if item]
    paths = sorted(set(paths))
    route = classify_paths(paths)
    if merge_group:
        route = Route(True, True, "MERGE_GROUP_RUN_REAL_ORACLES")
    elif force_real:
        route = Route(True, True, "MANUAL_DISPATCH_RUN_REAL_ORACLES")

    raw_diff = _git(
        repo,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        merge_base,
        head_sha,
        text=False,
    )
    assert isinstance(raw_diff, bytes)
    return {
        "schema": SCHEMA,
        "authority": False,
        "git": {
            "baseTip": base_sha,
            "base": merge_base,
            "head": head_sha,
            "tree": tree,
            "diffSha256": hashlib.sha256(raw_diff).hexdigest(),
        },
        "changedPaths": paths,
        "route": {
            "forcedReal": force_real,
            "productOraclesRequired": route.product,
            "guiPilotRequired": route.gui,
            "reason": route.reason,
            "productOracleCredit": "RUN_REQUIRED" if route.product else "EXPLICIT_NA_NON_PRODUCT_DIFF",
            "guiPilotCredit": "RUN_REQUIRED" if route.gui else "EXPLICIT_NA_NON_PRODUCT_DIFF",
        },
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_github_output(path: Path, receipt: dict) -> None:
    route = receipt["route"]
    lines = (
        f"product={'true' if route['productOraclesRequired'] else 'false'}\n"
        f"gui={'true' if route['guiPilotRequired'] else 'false'}\n"
        f"reason={route['reason']}\n"
        f"head={receipt['git']['head']}\n"
        f"tree={receipt['git']['tree']}\n"
        f"diff_sha256={receipt['git']['diffSha256']}\n"
    )
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--force-real", action="store_true")
    parser.add_argument("--merge-group", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base = args.base.strip()
    if not args.merge_group:
        base = base or "HEAD^"
    receipt = build_receipt(
        args.repo_root,
        base,
        args.head,
        force_real=args.force_real,
        merge_group=args.merge_group,
    )
    _atomic_json(args.receipt, receipt)
    if args.github_output is not None:
        _write_github_output(args.github_output, receipt)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
