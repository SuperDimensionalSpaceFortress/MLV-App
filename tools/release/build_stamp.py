#!/usr/bin/env python3
"""Generate and verify the compiled MLVApp provenance stamp."""
from __future__ import annotations
import argparse, hashlib, json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import os

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

SHA = re.compile(r"^[0-9a-f]{40}$")
MARKER = b"MLVAPP_BUILDSTAMP_v1|"
STAMP = re.compile(rb"MLVAPP_BUILDSTAMP_v1\|sha=([0-9a-f]{40})\|dirty=([01])(?=\x00|$)")

def git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW)
    except (OSError, subprocess.CalledProcessError) as e:
        raise RuntimeError(f"git {' '.join(args)} failed") from e
    return p.stdout

def generate(root: Path, expected: str, output: Path) -> None:
    if not SHA.fullmatch(expected): raise RuntimeError("expected SHA must be 40 lowercase hex characters")
    head = git(root, "rev-parse", "HEAD").strip()
    if head != expected or not SHA.fullmatch(head): raise RuntimeError("HEAD does not equal expected SHA")
    status = git(root, "status", "--porcelain")
    if status: raise RuntimeError(f"working tree is dirty: {status.strip()}")
    describe = git(root, "describe", "--always", "--dirty", "--abbrev=40").strip() or head
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Fixed-width octal escapes preserve UTF-8 bytes without C universal-character
    # restrictions or accidental consumption of following hex/octal characters.
    description_literal = '"' + ''.join(
        chr(b) if 32 <= b < 127 and b not in (34, 92) else f"\\{b:03o}"
        for b in describe.encode("utf-8")) + '"'
    body = f'''/* AUTO-GENERATED; do not edit or commit. */
#ifndef MLVAPP_BUILD_BUILDINFO_H
#define MLVAPP_BUILD_BUILDINFO_H
#ifdef MLVAPP_GIT_SHA
#undef MLVAPP_GIT_SHA
#endif
#define MLVAPP_GIT_SHA "{head}"
#define MLVAPP_BUILD_SHA "{head}"
#define MLVAPP_GIT_DIRTY 0
#define MLVAPP_GIT_DESCRIBE {description_literal}
#define MLVAPP_BUILD_TIME_UTC "{now}"
#define MLVAPP_BUILD_STAMP "MLVAPP_BUILDSTAMP_v1|sha={head}|dirty=0"
#endif
'''
    output.write_text(body, encoding="ascii", newline="\n")

def verify(binary: Path, expected: str) -> None:
    if not SHA.fullmatch(expected): raise RuntimeError("expected SHA must be 40 lowercase hex characters")
    data = binary.read_bytes()
    matches = list(STAMP.finditer(data))
    distinct = {(m.group(1), m.group(2)) for m in matches}
    if data.count(MARKER) != 1 or len(distinct) != 1 or len(matches) != 1:
        raise RuntimeError("missing, malformed or ambiguous build stamp")
    sha, dirty = next(iter(distinct))
    if sha.decode() != expected: raise RuntimeError("build stamp SHA mismatch")
    if dirty != b"0": raise RuntimeError("build stamp is dirty")
    receipt = {"binary": str(binary), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "sha": sha.decode(), "dirty": 0}
    print(json.dumps(receipt, sort_keys=True))

def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate"); g.add_argument("--repo-root", required=True); g.add_argument("--expected-sha", required=True); g.add_argument("--output-header", required=True)
    v = sub.add_parser("verify"); v.add_argument("--binary", required=True); v.add_argument("--expected-sha", required=True)
    try:
        a = ap.parse_args()
        if a.command == "generate": generate(Path(a.repo_root), a.expected_sha, Path(a.output_header))
        else: verify(Path(a.binary), a.expected_sha)
        return 0
    except (OSError, RuntimeError) as e:
        print(f"build_stamp: {e}", file=sys.stderr); return 1
if __name__ == "__main__": sys.exit(main())
