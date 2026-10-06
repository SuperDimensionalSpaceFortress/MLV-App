#!/usr/bin/env python3
"""Recall before diagnosis: rank fleet-doctrine entries against a symptom.

WHY. An ack on the doctrine bus only proves the commits were read; nothing retrieves the
lesson when the symptom shows up. On 2026-10-06 this board re-diagnosed the bachelor
PowerShell 5.1 SSH memory leak for about 3 hours although the fleet had published it on
2026-09-10. This tool closes that retrieval link: give it symptom words or a pasted log tail
and it prints the bus entries that already describe it, with their Remedy / Re-derive / Fix /
Prior art / Guard lines.

Read-only, offline, stdlib only. It never writes to the bus and never injects bus text into a
lane prompt (R15.2: lanes still get doctrine only through the Compose brief). It is a hub and
diagnosis tool.

Usage:
    py -3 tools/doctrine/doctrine_recall.py [--bus PATH] [--project-memory PATH] [--top N]
        [--json] "<symptom words or a pasted log tail>"      (use - to read the query from stdin)

Exit codes: 0 for every search (hits or none); 2 for a usage error or a missing bus.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUS = Path(r"C:\!Layi Wkspc\softwarefactory-fleet-doctrine")
CONSUMER_DOC = REPO_ROOT / "agents" / "doctrine-consumer.md"
BUS_FILES = ("TRAPS.md", "RECEIPTS.md", "RULINGS.md")
DEFAULT_TOP = 5
MAX_QUERY_TERMS = 30
# The brief allows 5 s for blame; 4 s keeps the whole run (about 0.4 s of search) under 5 s.
BLAME_BUDGET_S = 4.0
MAX_ENTRY_CHARS = 6000
CHUNK_CHARS = 3000
EXTRACT_MAX_CHARS = 240
SNIPPET_MAX_CHARS = 200
EXTRACT_RE = re.compile(r"^(Remedy|Re-derive|Fix|Prior art|Guard|Check)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?: [A-Za-z0-9_-]+){0,2}")
DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be but by can did do does for from had has have how if in into is it its "
    "no not of on or so than that the their then there these this to was we were what when which "
    "while who will with you your".split()
)
# BM25-style saturation and the extra weight a heading hit gets over a body hit.
BM25_K1 = 1.4
BM25_B = 0.5
HEADING_WEIGHT = 3
RECENCY_MAX_BONUS = 0.3
RECENCY_HALF_LIFE_DAYS = 120.0


@dataclass
class Entry:
    source: str  # "bus" or "project-memory"
    file: str
    line: int
    heading: str
    section: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _clean(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


def split_entries(text: str, source: str, file: str) -> list[Entry]:
    """Split markdown into entries.

    A `###` line starts an entry that owns everything up to the next `##` / `###`. A `##` line
    only opens a section (the bare literal `## TRAPS` is common): if prose follows it, the whole
    section is one entry; otherwise, while no `###` has been seen, each top-level `- ` bullet is
    its own entry, even a one-line one (the older TRAPS / RECEIPTS / RULINGS shape). Lines inside code fences never
    split anything.
    """
    out: list[Entry] = []
    section = ""
    in_h3 = False
    in_fence = False
    cur: Entry | None = None
    is_section_entry = False

    def close() -> None:
        nonlocal cur, is_section_entry
        if cur is not None:
            if is_section_entry:
                if any(ln.strip() for ln in cur.lines[1:]):
                    out.append(cur)
            else:
                out.append(cur)
        cur = None
        is_section_entry = False

    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if cur is not None:
                cur.lines.append(line)
            continue
        if in_fence:
            if cur is not None:
                cur.lines.append(line)
            continue
        heading = HEADING_RE.match(line)
        if heading and len(heading.group(1)) <= 3:
            level = len(heading.group(1))
            close()
            title = _clean(heading.group(2))
            if level <= 2:
                section = title
                in_h3 = False
                cur = Entry(source, file, number, title, section, [line])
                is_section_entry = True
            else:
                in_h3 = True
                cur = Entry(source, file, number, title, section, [line])
            continue
        bullet = BULLET_RE.match(line)
        if bullet and not in_h3 and not _has_prose(cur, is_section_entry):
            close()
            cur = Entry(source, file, number, _clean(bullet.group(1))[:160], section, [line])
            continue
        if cur is not None:
            cur.lines.append(line)
    close()
    return [piece for entry in out for piece in chunk_entry(entry)]


def chunk_entry(entry: Entry) -> list[Entry]:
    """Cut an oversized entry (some bare `## RECEIPTS` sections hold 80 KB) at blank lines.

    Each chunk keeps the entry's heading and carries its own line number, so a hit still points
    at the paragraph that matched rather than at the top of a huge section.
    """
    if len(entry.text) <= MAX_ENTRY_CHARS:
        return [entry]
    chunks: list[Entry] = []
    start = 0
    size = 0
    for index, line in enumerate(entry.lines):
        size += len(line) + 1
        boundary = size >= CHUNK_CHARS and (not line.strip() or size >= 2 * CHUNK_CHARS)
        if boundary or index == len(entry.lines) - 1:
            body = entry.lines[start : index + 1]
            if any(ln.strip() for ln in body):
                heading = entry.heading if start == 0 else f"{entry.heading} (cont.)"
                chunks.append(Entry(entry.source, entry.file, entry.line + start, heading, entry.section, body))
            start = index + 1
            size = 0
    return chunks


def _has_prose(cur: Entry | None, is_section_entry: bool) -> bool:
    """A `##` section that opens with prose is one entry; its bullets are part of that prose."""
    if cur is None or not is_section_entry:
        return False
    return any(ln.strip() and not BULLET_RE.match(ln) for ln in cur.lines[1:])


def _first_date(text: str) -> date | None:
    for match in DATE_RE.finditer(text):
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    return None


def entry_date(entry: Entry, today: date) -> date | None:
    """Heading date, else section date, else the newest date in the body (never in the future)."""
    for text in (entry.lines[0] if entry.lines else "", entry.heading, entry.section):
        found = _first_date(text)
        if found is not None:
            return found
    newest: date | None = None
    for match in DATE_RE.finditer(entry.text):
        try:
            found = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if found <= today and (newest is None or found > newest):
            newest = found
    return newest


def _tag_from(text: str) -> str:
    appended = re.search(r"Appended by ([^,(]+)", text)
    if appended:
        return appended.group(1).strip()
    for paren in re.finditer(r"\(([^)]*)\)", text):
        first = paren.group(1).split(",")[0].strip()
        if DATE_RE.fullmatch(first):
            lead = re.sub(r"^(?:TRAP|RECEIPT|RULING)\s+\S*\s*", "", text[: paren.start()]).strip()
            if lead and len(lead) <= 40 and not DATE_RE.search(lead):
                return lead
            continue
        if TAG_RE.fullmatch(first):
            return first
    return ""


def entry_project(entry: Entry) -> str:
    return _tag_from(entry.heading) or _tag_from(entry.section)


def tokenize(text: str) -> list[str]:
    out = []
    for token in TOKEN_RE.findall(text.lower()):
        if token in STOPWORDS or len(token) < 2 or (token.isdigit() and len(token) < 3):
            continue
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        out.append(token)
    return out


def extract_lines(entry: Entry) -> list[str]:
    found = []
    for line in entry.lines:
        body = re.sub(r"^\s*(?:[-*]\s+)?", "", line).replace("**", "")
        if EXTRACT_RE.match(body.strip()):
            found.append(_truncate(body.strip(), EXTRACT_MAX_CHARS))
    return found


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def load_entries(bus: Path | None, project_memory: Path | None) -> list[Entry]:
    entries: list[Entry] = []
    if bus is not None:
        for name in BUS_FILES:
            path = bus / name
            if path.is_file():
                entries += split_entries(path.read_text(encoding="utf-8", errors="replace"), "bus", name)
    if project_memory is not None and project_memory.is_dir():
        for path in sorted(project_memory.glob("*.md")):
            entries += split_entries(
                path.read_text(encoding="utf-8", errors="replace"), "project-memory", f"project-memory/{path.name}"
            )
    return entries


def rank(entries: list[Entry], query: str, today: date, top: int) -> tuple[list[str], list[tuple[float, Entry]]]:
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not entries:
        return query_terms, []
    counts: list[Counter] = []
    lengths: list[int] = []
    heading_counts: list[Counter] = []
    df: Counter = Counter()
    wanted = set(query_terms)
    for entry in entries:
        tokens = tokenize(entry.text)
        counter = Counter(tokens)
        counts.append(counter)
        lengths.append(max(len(tokens), 1))
        heading_counts.append(Counter(t for t in tokenize(entry.heading) if t in wanted))
        for term in wanted.intersection(counter):
            df[term] += 1
    total = len(entries)
    idf = {t: math.log(1.0 + (total - df[t] + 0.5) / (df[t] + 0.5)) for t in query_terms if df[t]}
    # A pasted log tail carries dozens of terms; keep the rarest ones so common words stay quiet.
    terms = sorted(idf, key=lambda t: (-idf[t], t))[:MAX_QUERY_TERMS]
    if not terms:
        return query_terms, []
    average = sum(lengths) / len(lengths)
    scored: list[tuple[float, int]] = []
    for index, counter in enumerate(counts):
        score = 0.0
        for term in terms:
            tf = counter.get(term, 0) + HEADING_WEIGHT * heading_counts[index].get(term, 0)
            if tf:
                norm = BM25_K1 * (1.0 - BM25_B + BM25_B * lengths[index] / average)
                score += idf[term] * tf * (BM25_K1 + 1.0) / (tf + norm)
        if score <= 0.0:
            continue
        when = entry_date(entries[index], today)
        if when is not None:
            age = max((today - when).days, 0)
            score *= 1.0 + RECENCY_MAX_BONUS * 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
        scored.append((score, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return terms, [(score, entries[index]) for score, index in scored[:top]]


def _run_git(args: list[str], deadline: float) -> str | None:
    """Run git with stdout in a temp file, never a pipe.

    A pipe plus a kill can hang forever on Windows: git.exe is a launcher, so killing it leaves
    the inner git holding the pipe (fleet TRAPS: "Async pipe reads plus WaitForExit hang git").
    On timeout the whole tree is killed with taskkill /T.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    with tempfile.TemporaryFile() as sink:
        try:
            proc = subprocess.Popen(
                ["git", *args], stdout=sink, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            )
        except OSError:
            return None
        try:
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            proc.kill()
            proc.wait()
            return None
        if code != 0:
            return None
        sink.seek(0)
        return sink.read().decode("utf-8", errors="replace")


def blame_commit(bus: Path, file: str, line: int, deadline: float) -> dict | None:
    out = _run_git(["-C", str(bus), "blame", "--porcelain", "-L", f"{line},{line}", "--", file], deadline)
    if not out:
        return None
    first = out.splitlines()[0].split()
    if not first or not re.fullmatch(r"[0-9a-f]{40}", first[0]):
        return None
    summary = next((ln[len("summary "):] for ln in out.splitlines() if ln.startswith("summary ")), "")
    return {"sha": first[0][:7], "summary": summary}


def attach_commits(bus: Path, hits: list[dict], budget_s: float) -> None:
    """One blame call per bus hit, in parallel, all sharing one deadline; failures are skipped."""
    if not (bus / ".git").exists():
        return
    deadline = time.monotonic() + budget_s
    targets = [hit for hit in hits if hit["source"] == "bus"]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(blame_commit, bus, hit["file"], hit["line"], deadline) for hit in targets]
        for hit, future in zip(targets, futures):
            try:
                hit["commit"] = future.result()
            except Exception:  # noqa: BLE001 - a blame failure must never fail a search
                hit["commit"] = None


def resolve_bus(explicit: str | None, consumer_doc: Path = CONSUMER_DOC) -> Path:
    if explicit:
        return Path(explicit)
    try:
        text = consumer_doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    named = re.search(r"`([A-Za-z]:\\[^`\r\n]*softwarefactory-fleet-doctrine[^`\r\n]*)`", text)
    return Path(named.group(1)) if named else DEFAULT_BUS


def hit_record(rank_no: int, score: float, entry: Entry, today: date) -> dict:
    when = entry_date(entry, today)
    extracts = extract_lines(entry)
    snippet = ""
    if not extracts:
        joined = _clean(" ".join(re.sub(r"^\s*(?:#{1,6}|[-*])\s+", "", ln) for ln in entry.lines if ln.strip()))
        joined = re.sub(r"\s+", " ", joined)
        if joined.startswith(entry.heading):
            joined = joined[len(entry.heading):].lstrip(" :-")
        snippet = _truncate(joined, SNIPPET_MAX_CHARS) if joined else ""
    return {
        "rank": rank_no,
        "score": round(score, 3),
        "source": entry.source,
        "file": entry.file,
        "line": entry.line,
        "date": when.isoformat() if when else None,
        "project": entry_project(entry),
        "heading": _truncate(entry.heading, 200),
        "extracts": extracts,
        "snippet": snippet,
        "commit": None,
    }


def search(
    query: str,
    bus: Path | None,
    project_memory: Path | None,
    top: int = DEFAULT_TOP,
    today: date | None = None,
    blame_budget_s: float = BLAME_BUDGET_S,
) -> dict:
    started = time.monotonic()
    today = today or date.today()
    entries = load_entries(bus, project_memory)
    terms, ranked = rank(entries, query, today, top)
    hits = [hit_record(i, score, entry, today) for i, (score, entry) in enumerate(ranked, 1)]
    if bus is not None and hits and blame_budget_s > 0:
        attach_commits(bus, hits, blame_budget_s)
    return {
        "query": query if len(query) <= 500 else query[:497] + "...",
        "terms": terms,
        "bus": str(bus) if bus is not None else None,
        "entries_searched": len(entries),
        "elapsed_s": round(time.monotonic() - started, 2),
        "hits": hits,
    }


def render(result: dict) -> str:
    lines = [
        f"doctrine-recall: {len(result['hits'])} hit(s) over {result['entries_searched']} entries "
        f"in {result['elapsed_s']} s; terms: {', '.join(result['terms']) or '(none)'}"
    ]
    if not result["hits"]:
        lines.append("recall: no prior art")
        return "\n".join(lines)
    for hit in result["hits"]:
        tag = f" [{hit['project']}]" if hit["project"] else ""
        lines.append(f"#{hit['rank']} score {hit['score']}  {hit['file']}:{hit['line']}  {hit['date'] or 'undated'}{tag}")
        lines.append(f"    {hit['heading']}")
        if hit.get("commit"):
            lines.append(f"    bus commit {hit['commit']['sha']}: {_truncate(hit['commit']['summary'], 100)}")
        for extract in hit["extracts"]:
            lines.append(f"    {extract}")
        if hit["snippet"]:
            lines.append(f"    {hit['snippet']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank fleet-doctrine bus entries against a symptom (read-only, offline). "
        "Cite the top hit before diagnosing, or write 'recall: no prior art'."
    )
    parser.add_argument("--bus", help="doctrine bus checkout (default: named in agents/doctrine-consumer.md)")
    parser.add_argument("--project-memory", help="project-memory dir (default: .claude-state/project-memory; skipped if absent)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"hits to show (default {DEFAULT_TOP})")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--blame-timeout", type=float, default=BLAME_BUDGET_S, help="total seconds for bus commit lookups; 0 disables"
    )
    parser.add_argument("query", nargs="+", help="symptom words or a pasted log tail; '-' reads stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)  # argparse exits 2 on a usage error
    query = sys.stdin.read() if args.query == ["-"] else " ".join(args.query)
    if not query.strip() or args.top < 1:
        print("doctrine-recall: need a non-empty query and --top >= 1", file=sys.stderr)
        return 2
    bus = resolve_bus(args.bus)
    if not bus.is_dir():
        print(f"doctrine-recall: bus not found: {bus} (pass --bus PATH)", file=sys.stderr)
        return 2
    memory = Path(args.project_memory) if args.project_memory else REPO_ROOT / ".claude-state" / "project-memory"
    result = search(query, bus, memory if memory.is_dir() else None, args.top, blame_budget_s=args.blame_timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
