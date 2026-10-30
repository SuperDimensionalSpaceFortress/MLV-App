#!/usr/bin/env python3
"""Check a project's markdown against its documentation size budgets (canonical factory tool).

One implementation for the fleet. Every project-specific value -- tiers, globs, exemptions, the
scan domain, the size unit, the ratchet, deferrals -- comes from `--config <json>`; this file names
no project path. It replaces three hand-ported rewrites (Conjugal, Cloudvore, magic-lantern) whose
behaviour it reproduces exactly when given their configs (tools/factory/check-doc-size.tests.py).

Budgets are assigned by ROLE, not by directory: the cost of a large file is proportional to how
often it is read.

Config (JSON; keys beginning with "_" are comments):
  schema            "factory-check-doc-size.v1"                                     (required)
  tiers             {name: {"soft": int, "hard": int, "why": str, "globs": [..]}}   (required)
  tier_order        [name, ...]  first tier whose glob matches wins                  (required)
  fallback_tier     tier for a governed path no glob matches        (default "working")
  exempt            [glob, ...]  never governed
  scan              "git-tracked" (git ls-files '*.md') | "worktree" (walk, untracked included)
  skip_dirs         [dirname, ...] pruned at any depth by the worktree walk; with
                    path_domain "refuse", --path/--headroom refuse such paths (exit 2)
  path_domain       "refuse" | "lenient" (default): may --path name a non-.md or skip-dir file?
  measure           "disk" (st_size, default) | "crlf" (size after LF->CRLF normalisation)
  exempt_ref        a git ref; every .md already present at that ref is exempt (inherited
                    upstream markdown). Unreadable ref => exempt nothing (degrades to governing).
  derived_entry     {"tier", "dir", "glob", "regex"}: the highest-sorting file in <dir> matching
                    <glob> and <regex> (tested against "<dir>/<name>") is forced into <tier>.
  baseline          {path: bytes} ratchet: may shrink, may not grow; GREW is a breach
  deferred          {glob: reason} measured and printed, excluded from exit status
  policy            text naming the policy of record (printed with breaches)
  remedy            [line, ...] printed when there is a breach

Globs use Python fnmatch semantics ('*' crosses '/'), which every one of the three rewrites used.

Exit status:
  0  no hard-cap breach among gated files
  1  at least one hard-cap breach, or a baselined file grew
  2  bad usage, unreadable config, a refused --path/--headroom, or the git listing failed
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "factory-check-doc-size.v1"
KNOWN_KEYS = {"schema", "tiers", "tier_order", "fallback_tier", "exempt", "scan", "skip_dirs",
              "path_domain", "measure", "exempt_ref", "derived_entry", "baseline", "deferred",
              "policy", "remedy"}
LF, CRLF = b"\n", b"\r\n"


class UsageError(Exception):
    pass


def load_config(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"config unreadable: {path} ({exc.__class__.__name__}: {exc})") from None
    if not isinstance(cfg, dict) or cfg.get("schema") != SCHEMA:
        raise UsageError(f"config {path}: schema must be {SCHEMA!r}")
    unknown = {k for k in cfg if not k.startswith("_")} - KNOWN_KEYS
    if unknown:
        raise UsageError(f"config {path}: unknown key(s) {sorted(unknown)} -- a typo here would silently govern nothing")
    tiers = cfg.get("tiers")
    order = cfg.get("tier_order")
    if not isinstance(tiers, dict) or not tiers or not isinstance(order, list) or set(order) - set(tiers):
        raise UsageError(f"config {path}: tiers must be a non-empty object and tier_order must name only its tiers")
    for name, t in tiers.items():
        if not (isinstance(t, dict) and type(t.get("soft")) is int and type(t.get("hard")) is int
                and 0 < t["soft"] <= t["hard"] and isinstance(t.get("globs", []), list)):
            raise UsageError(f"config {path}: tier {name!r} needs integer 0 < soft <= hard and a globs list")
    cfg.setdefault("fallback_tier", "working")
    if cfg["fallback_tier"] not in tiers:
        raise UsageError(f"config {path}: fallback_tier {cfg['fallback_tier']!r} is not a tier")
    cfg.setdefault("exempt", [])
    cfg.setdefault("scan", "git-tracked")
    cfg.setdefault("skip_dirs", [])
    cfg.setdefault("path_domain", "lenient")
    cfg.setdefault("measure", "disk")
    cfg.setdefault("baseline", {})
    cfg.setdefault("deferred", {})
    cfg.setdefault("remedy", [])
    if cfg["scan"] not in ("git-tracked", "worktree"):
        raise UsageError(f"config {path}: scan must be 'git-tracked' or 'worktree'")
    if cfg["measure"] not in ("disk", "crlf"):
        raise UsageError(f"config {path}: measure must be 'disk' or 'crlf'")
    if cfg["path_domain"] not in ("refuse", "lenient"):
        raise UsageError(f"config {path}: path_domain must be 'refuse' or 'lenient'")
    return cfg


class Checker:
    def __init__(self, cfg: dict, repo: Path, strict: bool = False):
        self.cfg, self.repo, self.strict = cfg, repo, strict
        self.skip = set(cfg["skip_dirs"])
        self.upstream = frozenset(self._ref_markdown(cfg.get("exempt_ref")))
        self.entry_extra = self._derived_entry(cfg.get("derived_entry"))

    # --- domain ---------------------------------------------------------------
    def _ref_markdown(self, ref: str | None) -> set[str]:
        if not ref:
            return set()
        p = subprocess.run(["git", "--no-optional-locks", "-C", str(self.repo), "ls-tree", "-r", "--name-only", ref],
                           capture_output=True, text=True)
        return {x for x in p.stdout.split("\n") if x.endswith(".md")} if p.returncode == 0 else set()

    def _derived_entry(self, spec: dict | None) -> dict[str, str]:
        if not spec:
            return {}
        d = self.repo / spec["dir"]
        if not d.is_dir():
            return {}
        rx = re.compile(spec.get("regex", ".*"))
        names = sorted(p.name for p in d.glob(spec["glob"]) if rx.match(f"{spec['dir']}/{p.name}"))
        return {f"{spec['dir']}/{names[-1]}": spec["tier"]} if names else {}

    def in_domain(self, rel: str) -> bool:
        if not rel.endswith(".md"):
            return False
        return not any(part in self.skip for part in rel.split("/")[:-1])

    def listing(self) -> list[str]:
        if self.cfg["scan"] == "worktree":
            out = []
            for root, dirs, files in os.walk(self.repo):
                dirs[:] = [d for d in dirs if d not in self.skip]
                out += [(Path(root) / n).relative_to(self.repo).as_posix() for n in files if n.endswith(".md")]
            return out
        p = subprocess.run(["git", "--no-optional-locks", "-C", str(self.repo), "ls-files", "-z", "*.md"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print("git ls-files failed:", p.stderr.strip(), file=sys.stderr)
            raise SystemExit(2)
        return [x for x in p.stdout.split("\0") if x]

    # --- verdict --------------------------------------------------------------
    def classify(self, rel: str) -> str | None:
        if rel in self.upstream:
            return None
        if any(fnmatch.fnmatch(rel, g) for g in self.cfg["exempt"]):
            return None
        if rel in self.entry_extra:
            return self.entry_extra[rel]
        for tier in self.cfg["tier_order"]:
            if any(fnmatch.fnmatch(rel, g) for g in self.cfg["tiers"][tier].get("globs", [])):
                return tier
        return self.cfg["fallback_tier"]

    def deferred(self, rel: str) -> str | None:
        for pattern, reason in self.cfg["deferred"].items():
            if fnmatch.fnmatch(rel, pattern):
                return reason
        return None

    def row(self, rel: str) -> dict | None:
        tier = self.classify(rel)
        if tier is None:
            return None
        try:
            raw = (self.repo / rel).read_bytes()
        except OSError:
            return None
        size = len(raw.replace(CRLF, LF).replace(LF, CRLF)) if self.cfg["measure"] == "crlf" else len(raw)
        t = self.cfg["tiers"][tier]
        state = "BREACH" if size > t["hard"] else "WARN" if size > t["soft"] else "ok"
        deferred = None if self.strict else self.deferred(rel)
        baseline = None if self.strict else self.cfg["baseline"].get(rel)
        if deferred and state != "ok":
            state = "DEFER"
        elif baseline is not None:
            state = "GREW" if size > baseline else "held"
        return {"path": rel, "bytes": size, "disk_bytes": len(raw), "tier": tier, "soft": t["soft"],
                "hard": t["hard"], "state": state, "baseline": baseline, "deferred": deferred}

    def normalise(self, raw: str) -> str | None:
        """Repo-relative POSIX path for a --path/--headroom argument; None when outside the repo."""
        rel = raw.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        candidate = Path(rel) if Path(rel).is_absolute() else self.repo / rel
        try:
            return str(candidate.resolve().relative_to(self.repo)).replace("\\", "/")
        except ValueError:
            # A "refuse" domain never measures a file outside the repo, however it is spelled (Cloudvore's
            # rewrite: --path skips it, --headroom exits 2). "lenient" keeps Conjugal's normpath of a
            # relative escape.
            if Path(rel).is_absolute() or self.cfg["path_domain"] == "refuse":
                return None
            return posixpath.normpath(rel)
        except OSError:
            return posixpath.normpath(rel)


def report(cfg: dict, rows: list[dict], args, notes: list[str]) -> int:
    breaches = [r for r in rows if r["state"] in ("BREACH", "GREW")]
    warns = [r for r in rows if r["state"] == "WARN"]
    deferred = [r for r in rows if r["state"] == "DEFER"]
    held = [r for r in rows if r["state"] == "held"]
    if args.json:
        print(json.dumps({"rows": rows, "breaches": len(breaches), "warnings": len(warns),
                          "deferred": len(deferred), "held": len(held)}, indent=2))
    elif not (args.quiet_ok and not breaches and not warns and not notes):
        for n in notes:
            print(n)
        shown = rows if args.all else breaches + warns
        if shown:
            print(f"{'bytes':>9}  {'tier':8} {'cap':>7}  {'state':6} path")
            for r in shown:
                line = f"{r['bytes']:>9,}  {r['tier']:8} {r['hard']:>7,}  {r['state']:6} {r['path']}"
                if r["disk_bytes"] != r["bytes"]:
                    line += f"  (on-disk {r['disk_bytes']:,})"
                print(line)
            print()
        if held:
            print(f"HELD (over cap, ratcheted - may shrink, may not grow): {len(held)}")
            for r in held:
                print(f"  {r['bytes']:>9,} / baseline {r['baseline']:,}  (cap {r['hard']:,})  {r['path']}")
            print()
        if deferred:
            print(f"DEFERRED (measured, NOT gated): {len(deferred)} files, {sum(r['bytes'] for r in deferred):,} bytes")
            for r in deferred:
                print(f"  {r['bytes']:>9,} / cap {r['hard']:,}  (over by {r['bytes'] - r['hard']:,})  {r['path']}")
            print()
        print(f"DOC SIZE: {len(rows)} governed files ({len(rows) - len(deferred)} gated, {len(deferred)} deferred), "
              f"{len(breaches)} breach, {len(warns)} over soft cap")
        if breaches:
            for line in cfg["remedy"]:
                print(line)
            if cfg.get("policy"):
                print(f"Policy of record: {cfg['policy']}")
    if args.warn_only:
        return 0
    return 1 if breaches else 0


def headroom(chk: Checker, raw: str, as_json: bool) -> int:
    rel = chk.normalise(raw)
    if rel is None:
        print(f"HEADROOM: {raw} is outside the repo {chk.repo}", file=sys.stderr)
        return 2
    if not chk.in_domain(rel):
        print(f"HEADROOM: {rel} is not in the gate's domain (not .md, or under a pruned directory) -- cannot tell",
              file=sys.stderr)
        return 2
    row = chk.row(rel)
    if row is None:
        print(f"HEADROOM: {rel} is not governed (exempt, or unreadable)", file=sys.stderr)
        return 2
    room = row["hard"] - row["bytes"]
    if as_json:
        print(json.dumps({**row, "headroom": room}, indent=2))
    else:
        print(f"path:      {row['path']}\ntier:      {row['tier']}")
        print(f"governed:  {row['bytes']:,} bytes  ({chk.cfg['measure']})")
        print(f"on-disk:   {row['disk_bytes']:,} bytes")
        print(f"soft cap:  {row['soft']:,} bytes\nhard cap:  {row['hard']:,} bytes")
        print(f"headroom:  {room:,} governed bytes remaining  ({row['state']})")
    return 1 if row["state"] in ("BREACH", "GREW") else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--config", required=True, help="the project's budget config (JSON)")
    ap.add_argument("--repo", default=".", help="repository root (default: cwd)")
    ap.add_argument("--all", action="store_true", help="list every governed file")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="ignore baseline and deferred; the unmitigated state")
    ap.add_argument("--warn-only", action="store_true", help="always exit 0; report rather than gate")
    ap.add_argument("--quiet-ok", action="store_true", help="print nothing when there is nothing to say")
    ap.add_argument("--path", action="append", default=[], help="check only these paths (repeatable)")
    ap.add_argument("--headroom", metavar="PATH", help="governed size, caps and remaining headroom for ONE file")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
    except UsageError as exc:
        print(f"check-doc-size: {exc}", file=sys.stderr)
        return 2
    repo = Path(args.repo).resolve()
    chk = Checker(cfg, repo, strict=args.strict)
    if args.headroom:
        return headroom(chk, args.headroom, args.json)
    notes: list[str] = []
    if args.path:
        rels = []
        for raw in args.path:
            rel = chk.normalise(raw)
            if rel is None:
                continue  # outside the repo; not ours to govern
            if cfg["path_domain"] == "refuse" and not chk.in_domain(rel):
                print(f"PATH: {rel} is not in the gate's domain (not .md, or under a pruned directory) -- cannot tell",
                      file=sys.stderr)
                return 2
            rels.append(rel)
    else:
        rels = chk.listing()
    rows = [r for r in (chk.row(rel) for rel in rels) if r]
    rows.sort(key=lambda r: (-r["bytes"], r["path"]))
    if args.path:
        notes = [f"not governed (exempt or missing): {rel}" for rel in rels if not any(r["path"] == rel for r in rows)]
    return report(cfg, rows, args, notes)


if __name__ == "__main__":
    raise SystemExit(main())
