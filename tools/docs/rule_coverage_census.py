#!/usr/bin/env python3
"""Census: which rule bullets in our contract docs have a check behind them?

Why it exists
    CLAUDE.md and agents/*.md state rules as list-item bullets. Nothing counted how many
    of those bullets are enforced by a guard, a test or a hook, so "the contract says X"
    and "something fails when X is violated" drifted apart without anyone noticing. The
    airmypc fleet measured the same gap (3 of 42 contract bullets had a check, 7.14%,
    and two unchecked rules had live incidents; bus TRAPS.md:28188). This tool adopts
    that idea only: a census, no pre-commit step, no PowerShell.

What it does
    1. Walks the rule surfaces at the working tree: CLAUDE.md and agents/*.md. Every
       list-item bullet (`-`, `*`, `+`, `1.`, `1)`, nested or not, outside code fences)
       is one rule. A bullet runs from its marker line through any wrapped continuation
       lines, up to a blank line, heading, fence, table row or the next marker.
    2. Gives each bullet a stable id: sha256 of its NORMALIZED text (see normalize()).
    3. Reads the tracked mapping file (tools/docs/rule-coverage-map.json) which maps an
       id to exactly one check, spelled as one of:
           guard:<NA-id>                a row of docs/never-authorized.json that has a
                                        real enforced_today (not "prose-only")
           test:<path>[::name[::name]]  a test file, optionally a def/class inside it
           hook:<path>                  a hook script that exists
           prose-only: <reason>         an admission that nothing checks it
    4. Prints coverage = (bullets mapped to a real check) / (all bullets). A prose-only
       label NEVER raises coverage; it only moves a bullet out of the unmapped list.
    5. Fails (exit 1) when a mapping is dishonest: it names a guard, test, test name or
       hook that no longer exists, a guard that is itself prose-only, or an id that no
       longer matches any bullet (STALE: the bullet was reworded or deleted, so whoever
       did that must re-point or drop the mapping). Report-only otherwise: an unmapped
       bullet is counted and listed, never a failure. The ratchet that would make a
       rising unmapped count fail is a later card (RULE-COVERAGE-RATCHET-1).

Normalization (the id contract; changing it re-ids every bullet, so do not)
    Applied to the bullet text with its list marker removed and continuation lines joined:
      1. Unicode NFC.
      2. Backtick code spans are exempt from emphasis stripping (step 3), nothing else.
      3. Outside code spans: markdown emphasis markers removed (`**x**`, `__x__`, `*x*`,
         `_x_`; single markers only when they hug a word, so snake_case and a lone `*`
         survive).
      4. All whitespace runs (spaces, tabs, newlines) collapsed to one space; trimmed.
    Whitespace inside a code span is collapsed too, so a code span wrapped across two lines
    keeps its id. Case and punctuation are preserved, so a reworded rule gets a new id on purpose.
    A whitespace-only or emphasis-only edit does not change the id.

Exit status:
  0  every mapping resolves (unmapped bullets are reported, not failed)
  1  at least one mapping is stale, dangling or dishonest
  2  a surface or the mapping file could not be read or parsed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

SCHEMA = "rule-coverage-census.v1"
MAP_SCHEMA = "rule-coverage-map.v1"
MAP_PATH = "tools/docs/rule-coverage-map.json"
GUARD_REGISTRY = "docs/never-authorized.json"
SURFACE_FILES = ("CLAUDE.md",)
SURFACE_DIRS = ("agents",)

_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_TABLE_RE = re.compile(r"^\s*\|")
_CODE_SPAN_RE = re.compile(r"(`[^`]*`)")
_STRONG_RE = re.compile(r"\*\*|__")
_STAR_EM_RE = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_UNDER_EM_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_PROSE_ONLY_RE = re.compile(r"^prose-only:\s*(\S.*)$", re.DOTALL)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/")


def normalize(text: str) -> str:
    """Return the canonical form hashed into a bullet id (see the module docstring)."""
    text = unicodedata.normalize("NFC", text)
    parts = _CODE_SPAN_RE.split(text)
    for i in range(0, len(parts), 2):  # even indices are outside code spans
        piece = _STRONG_RE.sub("", parts[i])
        piece = _STAR_EM_RE.sub(r"\1", piece)
        piece = _UNDER_EM_RE.sub(r"\1", piece)
        parts[i] = piece
    return " ".join("".join(parts).split())


def bullet_id(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def surface_paths(repo_root: str):
    """Repo-relative rule surfaces that exist, in a stable order."""
    paths = [p for p in SURFACE_FILES if os.path.isfile(os.path.join(repo_root, p))]
    for d in SURFACE_DIRS:
        full = os.path.join(repo_root, d)
        if os.path.isdir(full):
            paths.extend("%s/%s" % (d, n) for n in sorted(os.listdir(full))
                         if n.endswith(".md"))
    return paths


def extract_bullets(body: str):
    """Yield (line_number, raw_text) for every list-item bullet outside code fences."""
    lines = body.splitlines()
    fence = False
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            fence = not fence
            i += 1
            continue
        m = None if fence else _ITEM_RE.match(line)
        if not m:
            i += 1
            continue
        chunk = [m.group(2)]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if (not nxt.strip() or _ITEM_RE.match(nxt) or _FENCE_RE.match(nxt)
                    or _HEADING_RE.match(nxt) or _TABLE_RE.match(nxt)):
                break
            chunk.append(nxt)
            j += 1
        out.append((i + 1, "\n".join(chunk)))
        i = j
    return out


def collect_bullets(repo_root: str):
    """Every bullet on every surface: [{file, line, id, text}]. Raises OSError on read."""
    bullets = []
    for rel in surface_paths(repo_root):
        with open(os.path.join(repo_root, rel), encoding="utf-8-sig") as fh:
            body = fh.read()
        for line, raw in extract_bullets(body):
            bullets.append({"file": rel, "line": line, "id": bullet_id(raw),
                            "text": normalize(raw)})
    return bullets


def load_map(repo_root: str, map_path: str | None = None):
    """Return ({id: entry}, error). A missing map file is an empty map, not an error."""
    path = map_path or os.path.join(repo_root, MAP_PATH)
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read mapping file %s: %s" % (path, exc)
    if not isinstance(data, dict) or not isinstance(data.get("mappings"), dict):
        return None, "mapping file %s must be an object with a 'mappings' object" % path
    return data["mappings"], None


def load_guards(repo_root: str):
    """{guard id: enforced_today text} from docs/never-authorized.json, or ({}, error)."""
    path = os.path.join(repo_root, GUARD_REGISTRY)
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read %s: %s" % (GUARD_REGISTRY, exc)
    guards = {}
    for item in data.get("items", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            enforced = item.get("enforced_today")
            guards[item["id"]] = enforced if isinstance(enforced, str) else ""
    return guards, None


def _safe_file(repo_root: str, rel: str):
    """Absolute path of a repo-relative file, or None when it escapes or is absent."""
    rel = _norm_path(rel.strip())
    if not rel or rel.startswith("/") or ":" in rel or ".." in rel.split("/"):
        return None
    full = os.path.join(repo_root, rel)
    return full if os.path.isfile(full) else None


def _name_present(path: str, name: str) -> bool:
    name = name.split("[", 1)[0].strip()
    if not name:
        return False
    try:
        with open(path, encoding="utf-8-sig") as fh:
            body = fh.read()
    except OSError:
        return False
    if path.endswith(".py"):
        return re.search(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+%s\b" % re.escape(name),
                         body) is not None
    return name in body


def classify(check, repo_root: str, guards):
    """Resolve one mapping value. Returns (kind, error): kind in
    guard|test|hook|prose-only, or None with an error string."""
    if isinstance(check, dict):
        check = check.get("check")
    if not isinstance(check, str) or not check.strip():
        return None, "mapping has no check string"
    check = check.strip()
    prose = _PROSE_ONLY_RE.match(check)
    if prose:
        return "prose-only", None
    if check.startswith("prose-only"):
        return None, "prose-only needs a reason: 'prose-only: <reason>'"
    if check.startswith("guard:"):
        gid = check[len("guard:"):].strip()
        if gid not in guards:
            return None, "guard %r is not in %s" % (gid, GUARD_REGISTRY)
        if guards[gid].strip().lower().startswith("prose-only"):
            return None, ("guard %r is itself prose-only today, so it checks nothing"
                          % gid)
        return "guard", None
    if check.startswith("test:"):
        spec = check[len("test:"):].strip()
        rel, *names = [s.strip() for s in spec.split("::")]
        full = _safe_file(repo_root, rel)
        if not full:
            return None, "test file %r does not exist" % rel
        for n in names:
            if not _name_present(full, n):
                return None, "test %r has no definition named %r" % (rel, n)
        return "test", None
    if check.startswith("hook:"):
        rel = check[len("hook:"):].strip()
        if not _safe_file(repo_root, rel):
            return None, "hook file %r does not exist" % rel
        return "hook", None
    return None, ("unrecognized check %r (use guard:, test:, hook: or prose-only:)"
                  % check[:60])


def census(repo_root: str, map_path: str | None = None):
    """Compute the whole census. Returns (result dict, fatal error or None)."""
    try:
        bullets = collect_bullets(repo_root)
    except OSError as exc:
        return None, "cannot read a rule surface: %s" % exc
    mapping, err = load_map(repo_root, map_path)
    if mapping is None:
        return None, err
    guards, err = load_guards(repo_root)
    if guards is None:
        return None, err

    by_id = {}
    for b in bullets:
        by_id.setdefault(b["id"], []).append(b)

    errors = []
    kinds = {}
    for mid, entry in sorted(mapping.items()):
        kind, why = classify(entry, repo_root, guards)
        if mid not in by_id:
            errors.append({"id": mid, "why": "stale: no bullet has this id any more "
                           "(reworded or removed); re-point or drop the mapping"})
            continue
        if why:
            errors.append({"id": mid, "why": why})
            continue
        kinds[mid] = kind

    counts = {"guard": 0, "test": 0, "hook": 0, "prose-only": 0}
    unmapped = []
    for b in bullets:
        kind = kinds.get(b["id"])
        b["status"] = ("mapped" if kind in ("guard", "test", "hook")
                       else "prose-only" if kind == "prose-only" else "unmapped")
        if kind:
            counts[kind] += 1
        if b["status"] == "unmapped":
            unmapped.append(b)
    total = len(bullets)
    mapped = counts["guard"] + counts["test"] + counts["hook"]
    per_file = {}
    for b in bullets:
        row = per_file.setdefault(b["file"], {"bullets": 0, "mapped": 0})
        row["bullets"] += 1
        row["mapped"] += 1 if b["status"] == "mapped" else 0
    return {
        "schema": SCHEMA,
        "total": total,
        "mapped": mapped,
        "mapped_by_kind": {k: counts[k] for k in ("guard", "test", "hook")},
        "prose_only": counts["prose-only"],
        "unmapped": len(unmapped),
        "coverage_percent": round(100.0 * mapped / total, 2) if total else 0.0,
        "duplicate_ids": sorted(i for i, v in by_id.items() if len(v) > 1),
        "per_file": per_file,
        "errors": errors,
        "bullets": bullets,
    }, None


def _print_report(res, show_unmapped: bool):
    print("rule-coverage census -- %d bullets across %d surfaces"
          % (res["total"], len(res["per_file"])))
    for path, row in sorted(res["per_file"].items()):
        print("  %-48s %3d bullets  %3d mapped" % (path, row["bullets"], row["mapped"]))
    print()
    print("  bullets            %d" % res["total"])
    print("  mapped to a check  %d  (guard %d, test %d, hook %d)"
          % ((res["mapped"],) + tuple(res["mapped_by_kind"][k]
                                      for k in ("guard", "test", "hook"))))
    print("  prose-only         %d  (never counted as coverage)" % res["prose_only"])
    print("  unmapped           %d" % res["unmapped"])
    print("  coverage           %.2f%%  (%d of %d)"
          % (res["coverage_percent"], res["mapped"], res["total"]))
    if res["duplicate_ids"]:
        print("  note: %d id(s) shared by identical bullets" % len(res["duplicate_ids"]))
    print()
    if show_unmapped and res["unmapped"]:
        print("UNMAPPED (%d):" % res["unmapped"])
        for b in res["bullets"]:
            if b["status"] == "unmapped":
                print("  %s:%d  %s  %s" % (b["file"], b["line"], b["id"][:12],
                                           b["text"][:70]))
        print()
    if res["errors"]:
        print("MAPPING ERRORS (%d) -- fix %s:" % (len(res["errors"]), MAP_PATH))
        for e in res["errors"]:
            print("  %s  %s" % (e["id"][:12], e["why"]))
    else:
        print("PASS -- every mapping resolves.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--map", default=None, help="mapping file (default %s)" % MAP_PATH)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--no-list", action="store_true",
                    help="omit the per-bullet unmapped list")
    args = ap.parse_args(argv)

    # Bullet text may hold characters a legacy console codepage cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    repo_root = args.repo_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    repo_root = os.path.abspath(repo_root)

    res, err = census(repo_root, args.map)
    if err:
        sys.stderr.write("%s\n" % err)
        return 2
    if args.as_json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res, show_unmapped=not args.no_list)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
