#!/usr/bin/env python3
"""Recall before diagnosis: rank fleet-doctrine entries against a symptom.

WHY. An ack on the doctrine bus only proves the commits were read; nothing retrieves the
lesson when the symptom shows up. On 2026-10-06 this board re-diagnosed the bachelor
PowerShell 5.1 SSH memory leak for about 3 hours although the fleet had published it on
2026-09-10. This tool closes that retrieval link: give it symptom words or a pasted log tail
and it prints the bus entries that already describe it, with their Remedy / Re-derive / Fix /
Prior art / Guard lines.

Read-only, stdlib only. It never writes bus content and never injects bus text into a lane prompt
(R15.2: lanes still get doctrine only through the Compose brief). It is a hub and diagnosis tool.
The one thing it touches is the bus clone's remote-tracking ref origin/master, through a bounded
`git fetch` (see STALE CLONE); it never pulls, rebases, commits or checks out in the shared clone.

STALE CLONE. The local bus clone's HEAD only moves when a heartbeat writer runs `pull --rebase`, so
HEAD can trail origin/master by days and both recall and fold debt would read a bus that is
missing the newest entries. Both therefore do a bounded `git fetch origin` first (--fetch-timeout,
at most 30 s; --no-fetch skips it). A failed fetch is never silent: a `FETCH_FAILED <reason>` line
is printed (stderr for --fold-debt, the first line of the text output for a search) and the tool
falls back to what it has. Fold debt prices `since..HEAD` plus `since..origin/master`. Recall reads
the corpus files at origin/master (`git show`) while HEAD is strictly behind it, and prints
`BEHIND=<n>` so the reader knows the working tree is not what was searched.

CORPUS. TRAPS.md, RECEIPTS.md and RULINGS.md, plus the cards channel (bus ruling R14): every
specs/<project>/cards.md of every OTHER project (this project's own cards are its own words, so
they are skipped). One `## <project>/<slug>` card is one entry; its hit names the cards file and
line and reports the owning project (the directory name). A bus with only cards files is a
corpus; a bus with none of the four kinds is still refused.

FOLD DEBT. `--fold-debt --since SHA` is the input of the DOCTRINE-FOLD-CADENCE-1 cadence: it lists,
as JSON, the bus content commits after SHA (SHA..HEAD) that touched TRAPS.md, RECEIPTS.md,
RULINGS.md or another project's specs/<project>/cards.md: sha, date, subject and the counted
files, plus a count. A commit that only touched this project's own cards is not debt. Bounded git
calls under the same deadline helper as blame.

Usage:
    py -3 tools/doctrine/doctrine_recall.py [--bus PATH] [--project-memory PATH] [--top N]
        [--fetch-timeout S | --no-fetch] [--json] "<symptom words or a pasted log tail>"  (- reads stdin)
    py -3 tools/doctrine/doctrine_recall.py [--bus PATH] [--git-timeout S] [--fetch-timeout S | --no-fetch]
        --fold-debt --since SHA

Exit codes: 0 for every search (hits or none) and for a fold-debt listing; 2 for a usage error, a
missing bus, or a bus directory with no corpus (none of TRAPS.md / RECEIPTS.md / RULINGS.md /
another project's cards, or no entries parsed); 3 for --fold-debt with a SHA the bus does not
know; 4 for --fold-debt when the bus is not a git checkout or git fails or times out.
Writes nothing to disk: bus commit lookups read git blame through a pipe under one deadline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
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
CARDS_GLOB = "specs/*/cards.md"
CARDS_PATH_RE = re.compile(r"^specs/([^/]+)/cards\.md$")
DEFAULT_OWN_PROJECT = "mlv-app"
OWN_PROJECT_RE = re.compile(r"`specs/([a-z0-9][a-z0-9-]*)\.md`")
FOLD_DEBT_BUDGET_S = 10.0
BUS_REF = "origin/master"
BUS_FETCH_REFSPEC = "+refs/heads/master:refs/remotes/origin/master"  # forced: a plain fetch can leave the ref stale
FETCH_MAX_S = 30.0
FETCH_BUDGET_FOLD_DEBT_S = 30.0
FETCH_BUDGET_SEARCH_S = 8.0
READ_AT_REF_BUDGET_S = 10.0
EXIT_UNKNOWN_SHA = 3
EXIT_FOLD_DEBT_GIT = 4
SHA_RE = re.compile(r"[0-9a-fA-F]{4,40}")
DEFAULT_TOP = 5
MAX_QUERY_TERMS = 30
# The brief allows 5 s for blame. Blame's worst case is this budget plus KILL_GRACE_S of cleanup
# (4.5 s), so with about 0.4 s of search the whole run stays under 5 s. The oldest real-bus hit
# (TRAPS.md line 8336) needs about 2.8 s of blame on its own, which is why the budget is not lower.
BLAME_BUDGET_S = 4.0
KILL_GRACE_S = 0.5  # past the deadline: taskkill plus the final communicate, together
KILL_TASKKILL_S = 0.3
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


def card_project(path: str) -> str:
    """The owning project of a specs/<project>/cards.md path, or "" for any other file."""
    match = CARDS_PATH_RE.match(path.replace("\\", "/"))
    return match.group(1) if match else ""


def entry_project(entry: Entry) -> str:
    return card_project(entry.file) or _tag_from(entry.heading) or _tag_from(entry.section)


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


def own_project(consumer_doc: Path | None = None) -> str:
    """This project's id on the bus: the `specs/<id>.md` spec the consumer doc names."""
    try:
        text = (consumer_doc or CONSUMER_DOC).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return DEFAULT_OWN_PROJECT
    named = OWN_PROJECT_RE.search(text)
    return named.group(1) if named else DEFAULT_OWN_PROJECT


def card_files(bus: Path, own: str) -> list[Path]:
    """Every other project's cards file in the bus checkout, sorted, this project's own excluded."""
    return [
        path
        for path in sorted(bus.glob(CARDS_GLOB))
        if path.is_file() and path.parent.name.lower() != own.lower()
    ]


def load_entries(
    bus: Path | None, project_memory: Path | None, own: str | None = None, ref_texts: dict[str, str] | None = None
) -> list[Entry]:
    """Bus entries from the working tree, or from ref_texts (read_bus_at_ref) when the clone trails origin."""
    entries: list[Entry] = []
    if bus is not None and ref_texts is not None:
        own_id = (own if own is not None else own_project()).lower()
        for name in BUS_FILES:
            if name in ref_texts:
                entries += split_entries(ref_texts[name], "bus", name)
        for rel in sorted(ref_texts):
            owner = card_project(rel)
            if owner and owner.lower() != own_id:
                entries += split_entries(ref_texts[rel], "bus", rel)
    elif bus is not None:
        for name in BUS_FILES:
            path = bus / name
            if path.is_file():
                entries += split_entries(path.read_text(encoding="utf-8", errors="replace"), "bus", name)
        for path in card_files(bus, own if own is not None else own_project()):
            rel = f"specs/{path.parent.name}/cards.md"
            entries += split_entries(path.read_text(encoding="utf-8", errors="replace"), "bus", rel)
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


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill git and, on Windows, the inner git its launcher spawned (bounded to KILL_TASKKILL_S)."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=KILL_TASKKILL_S,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _git_run(
    args: list[str], deadline: float, capture_stderr: bool = False, env: dict | None = None
) -> tuple[str | None, str, str]:
    """Run git with stdout on a pipe; return (stdout, status, stderr), status "ok" | "timeout" | "error".

    stderr is "" unless capture_stderr is set.

    No filesystem writes. Everything is bounded by ONE shared monotonic deadline: communicate()
    waits at most until it; on expiry the process tree is killed and one more communicate() waits
    at most KILL_GRACE_S past the deadline (taskkill's own timeout is inside that grace, so the
    worst case is the budget plus KILL_GRACE_S, 4.5 s by default). If the
    inner git still holds the pipe after that, the lookup is abandoned as a timeout rather than
    waited for: nothing in here blocks without a timeout.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "timeout", ""
    extra = {"env": {**os.environ, **env}} if env else {}
    try:
        proc = subprocess.Popen(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **extra,
        )
    except OSError as exc:
        return None, "error", str(exc)
    try:
        stdout, stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=max(deadline + KILL_GRACE_S - time.monotonic(), 0.05))
        except subprocess.TimeoutExpired:
            pass
        return None, "timeout", ""
    text = stderr.decode("utf-8", errors="replace") if stderr else ""
    if proc.returncode != 0:
        return None, "error", text
    return stdout.decode("utf-8", errors="replace"), "ok", text


def _git_output(args: list[str], deadline: float) -> tuple[str | None, str]:
    """(stdout, status) of _git_run, status "ok" | "timeout" | "error"; stderr is discarded."""
    out, status, _ = _git_run(args, deadline)
    return out, status


def _run_git(args: list[str], deadline: float) -> str | None:
    return _git_output(args, deadline)[0]


def _fetch_failed(reason: str) -> str:
    return "FETCH_FAILED " + (" ".join(reason.split())[:200] or "unknown")


def sync_bus(bus: Path, fetch_budget_s: float) -> dict:
    """Bounded `git fetch origin` of master, then how far HEAD sits from origin/master.

    Returns {"fetch", "behind", "ahead", "ref"}. fetch is "ok", "skipped" (budget 0), "no-git",
    "no-origin" (a local-only bus cannot lag) or "FETCH_FAILED <reason>". behind/ahead count the commits
    HEAD lacks / origin/master lacks, from the origin/master ref as it stands after the fetch (the last
    successful fetch when this one failed); ref is BUS_REF when that ref resolves, else None. Writes only
    refs/remotes/origin/master: no pull, rebase, checkout, FETCH_HEAD, tags or auto-gc in the shared clone.
    """
    state = {"fetch": "skipped", "behind": 0, "ahead": 0, "ref": None}
    if not (bus / ".git").exists():
        state["fetch"] = "no-git"
        return state
    deadline = time.monotonic() + min(max(fetch_budget_s, 0.0), FETCH_MAX_S)
    _, status = _git_output(["-C", str(bus), "config", "--get", "remote.origin.url"], deadline + 5.0)
    if status == "error":
        state["fetch"] = "no-origin"
        return state
    if status == "timeout":
        state["fetch"] = _fetch_failed("timeout reading remote.origin.url")
    elif fetch_budget_s > 0:
        _, status, err = _git_run(
            ["-C", str(bus), "-c", "gc.auto=0", "fetch", "--no-tags", "--no-write-fetch-head", "origin", BUS_FETCH_REFSPEC],
            deadline,
            capture_stderr=True,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if status == "ok":
            state["fetch"] = "ok"
        elif status == "timeout":
            state["fetch"] = _fetch_failed(f"timeout after {fetch_budget_s:g} s")
        else:
            lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
            state["fetch"] = _fetch_failed(lines[-1] if lines else "git fetch exited non-zero")
    counts, status = _git_output(
        ["-C", str(bus), "rev-list", "--left-right", "--count", f"HEAD...{BUS_REF}"], time.monotonic() + 5.0
    )
    parts = counts.split() if status == "ok" and counts else []
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        state["ahead"], state["behind"], state["ref"] = int(parts[0]), int(parts[1]), BUS_REF
    elif state["fetch"] == "ok":
        state["fetch"] = _fetch_failed(f"{BUS_REF} does not resolve after the fetch")
    return state


def read_bus_at_ref(bus: Path, ref: str, budget_s: float = READ_AT_REF_BUDGET_S) -> dict[str, str] | None:
    """The bus corpus files (TRAPS/RECEIPTS/RULINGS and specs/*/cards.md) as committed at ref, or None."""
    deadline = time.monotonic() + budget_s
    listing, status = _git_output(
        ["-C", str(bus), "ls-tree", "-r", "-z", "--name-only", ref, "--", *BUS_FILES, "specs"], deadline
    )
    if status != "ok" or listing is None:
        return None
    texts: dict[str, str] = {}
    for name in sorted(n for n in listing.split("\0") if n in BUS_FILES or card_project(n)):
        text, status = _git_output(["-C", str(bus), "show", f"{ref}:{name}"], deadline)
        if status != "ok" or text is None:
            return None
        texts[name] = text
    return texts


def blame_lookup(bus: Path, file: str, line: int, deadline: float, rev: str | None = None) -> tuple[dict | None, str]:
    out, status = _git_output(
        ["-C", str(bus), "blame", "--porcelain", "-L", f"{line},{line}", *([rev] if rev else []), "--", file], deadline
    )
    if status != "ok":
        return None, status
    first = out.splitlines()[0].split() if out else []
    if not first or not re.fullmatch(r"[0-9a-f]{40}", first[0]):
        return None, "error"
    summary = next((ln[len("summary "):] for ln in out.splitlines() if ln.startswith("summary ")), "")
    return {"sha": first[0][:7], "summary": summary}, "ok"


def blame_commit(bus: Path, file: str, line: int, deadline: float) -> dict | None:
    return blame_lookup(bus, file, line, deadline)[0]


def attach_commits(bus: Path, hits: list[dict], budget_s: float, rev: str | None = None) -> None:
    """One blame call per bus hit, in parallel, all sharing one deadline; every outcome is labelled.

    commit_status is "ok", "timeout", "error" or "no-git" (the bus is not a git checkout), so a
    missing commit is never mistaken for a bus that simply has none.
    """
    targets = [hit for hit in hits if hit["source"] == "bus"]
    if not targets:
        return
    if not (bus / ".git").exists():
        for hit in targets:
            hit["commit_status"] = "no-git"
        return
    deadline = time.monotonic() + budget_s
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(blame_lookup, bus, hit["file"], hit["line"], deadline, rev) for hit in targets]
        for hit, future in zip(targets, futures):
            try:
                hit["commit"], hit["commit_status"] = future.result()
            except Exception:  # noqa: BLE001 - a blame failure must never fail a search
                hit["commit"], hit["commit_status"] = None, "error"


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
        "commit_status": "not-requested",
    }


class CorpusUnavailable(Exception):
    """The bus directory exists but yielded no bus entries, so a search would prove nothing."""


def search(
    query: str,
    bus: Path | None,
    project_memory: Path | None,
    top: int = DEFAULT_TOP,
    today: date | None = None,
    blame_budget_s: float = BLAME_BUDGET_S,
    own: str | None = None,
    sync: dict | None = None,
) -> dict:
    """Rank the corpus against query. sync (from sync_bus) says whether the clone trails origin/master."""
    started = time.monotonic()
    today = today or date.today()
    ref_texts = None
    if bus is not None and sync and sync["ref"] and sync["behind"] > 0 and sync["ahead"] == 0:
        ref_texts = read_bus_at_ref(bus, sync["ref"])
    read_ref = sync["ref"] if ref_texts is not None and sync else None
    entries = load_entries(bus, project_memory, own, ref_texts)
    if bus is not None and not any(entry.source == "bus" for entry in entries):
        raise CorpusUnavailable(bus)
    terms, ranked = rank(entries, query, today, top)
    hits = [hit_record(i, score, entry, today) for i, (score, entry) in enumerate(ranked, 1)]
    if bus is not None and hits and blame_budget_s > 0:
        attach_commits(bus, hits, blame_budget_s, read_ref)
    return {
        "query": query if len(query) <= 500 else query[:497] + "...",
        "terms": terms,
        "bus": str(bus) if bus is not None else None,
        "entries_searched": len(entries),
        "elapsed_s": round(time.monotonic() - started, 2),
        "bus_state": {**sync, "read": read_ref or "HEAD"} if sync else None,
        "hits": hits,
    }


def _bus_state_lines(state: dict | None) -> list[str]:
    """FETCH_FAILED / BEHIND lines for the head of a text report; empty when the clone is current."""
    if not state:
        return []
    lines = []
    failed = state["fetch"].startswith("FETCH_FAILED")
    if failed:
        lines.append(state["fetch"])
    if state["behind"] > 0:
        if state["ahead"] > 0:
            note = f"AHEAD={state['ahead']}: diverged from {BUS_REF}, entries read at HEAD"
        elif state["read"] == BUS_REF:
            note = f"bus HEAD trails {BUS_REF}; entries read at {BUS_REF}"
        else:
            note = f"bus HEAD trails {BUS_REF} and it could not be read; entries read at HEAD"
        if failed:
            note += f"; {BUS_REF} is as of the last good fetch"
        lines.append(f"BEHIND={state['behind']} ({note})")
    return lines


def render(result: dict) -> str:
    lines = _bus_state_lines(result.get("bus_state"))
    lines += [
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
        elif hit.get("commit_status") in ("timeout", "error", "no-git"):
            lines.append(f"    bus commit: unavailable ({hit['commit_status']})")
        for extract in hit["extracts"]:
            lines.append(f"    {extract}")
        if hit["snippet"]:
            lines.append(f"    {hit['snippet']}")
    return "\n".join(lines)


def _fold_debt_files(files: list[str], own: str) -> list[str]:
    """The files of one commit that count as bus content debt (this project's own cards excluded)."""
    counted = []
    for name in files:
        owner = card_project(name)
        if name in BUS_FILES or (owner and owner.lower() != own.lower()):
            counted.append(name)
    return counted


def fold_debt(
    bus: Path, since: str, own: str, budget_s: float = FOLD_DEBT_BUDGET_S, fetch_budget_s: float = FETCH_BUDGET_FOLD_DEBT_S
) -> tuple[dict | None, int, str]:
    """Bus content commits after since, on HEAD and on origin/master, touching TRAPS/RECEIPTS/RULINGS or another project's cards.

    A bounded fetch comes first (sync_bus). When it succeeds, origin/master is priced alongside HEAD,
    so commits the clone has not pulled still count; when it fails, only HEAD is priced and the result's
    "fetch" field says FETCH_FAILED <reason>. Returns (result, exit_code, message). Never pulls or
    checks out: a bounded fetch plus three bounded git calls on one shared deadline.
    """
    if not (bus / ".git").exists():
        return None, EXIT_FOLD_DEBT_GIT, f"bus is not a git checkout: {bus}"
    sync = sync_bus(bus, fetch_budget_s)
    priced = ["HEAD", BUS_REF] if sync["fetch"] == "ok" and sync["ref"] else ["HEAD"]
    deadline = time.monotonic() + budget_s
    resolved = None
    if SHA_RE.fullmatch(since):
        out, status = _git_output(["-C", str(bus), "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}"], deadline)
        if status == "timeout":
            return None, EXIT_FOLD_DEBT_GIT, "git timed out resolving the cursor sha"
        resolved = out.strip() if status == "ok" and out else None
    if not resolved:
        return None, EXIT_UNKNOWN_SHA, f"unknown sha {since!r}: not a commit in {bus}"
    head_out, head_status = _git_output(["-C", str(bus), "rev-parse", "HEAD"], deadline)
    log_out, log_status = _git_output(
        [
            "-C", str(bus), "log", "-z", "--name-only", "--no-renames", "--format=%x1e%H%x1f%cI%x1f%s",
            *priced, f"^{resolved}", "--", *BUS_FILES, CARDS_GLOB,
        ],
        deadline,
    )
    if head_status != "ok" or log_status != "ok" or log_out is None:
        return None, EXIT_FOLD_DEBT_GIT, f"git log failed ({log_status if log_status != 'ok' else head_status})"
    commits = []
    for record in log_out.split("\x1e"):
        if not record.strip():
            continue
        # -z: the header ends at a NUL, then one newline, then each path ends at a NUL (never quoted).
        header, _, names = record.partition("\0")
        parts = header.split("\x1f")
        if len(parts) != 3:
            continue
        counted = _fold_debt_files([n for n in names.removeprefix("\n").split("\0") if n], own)
        if counted:
            commits.append({"sha": parts[0], "date": parts[1], "files": counted, "subject": parts[2]})
    return {
        "bus": str(bus),
        "since": since,
        "since_commit": resolved,
        "head": (head_out or "").strip(),
        "priced": priced,
        "fetch": sync["fetch"],
        "behind": sync["behind"],
        "ahead": sync["ahead"],
        "own_project": own,
        "count": len(commits),
        "commits": commits,
    }, 0, ""


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
    parser.add_argument(
        "--fold-debt", action="store_true",
        help="print, as JSON, the bus content commits since --since that touch TRAPS/RECEIPTS/RULINGS or another project's cards",
    )
    parser.add_argument("--since", help="with --fold-debt: the cursor commit; commits after it count")
    parser.add_argument(
        "--git-timeout", type=float, default=FOLD_DEBT_BUDGET_S, help="with --fold-debt: total seconds for the git calls"
    )
    parser.add_argument(
        "--fetch-timeout", type=float, default=None,
        help=f"seconds for the bounded `git fetch origin` that runs first (max {FETCH_MAX_S:g}; default "
        f"{FETCH_BUDGET_FOLD_DEBT_S:g} with --fold-debt, {FETCH_BUDGET_SEARCH_S:g} for a search)",
    )
    parser.add_argument("--no-fetch", action="store_true", help="skip the fetch; work from the last fetched origin/master")
    parser.add_argument("query", nargs="*", help="symptom words or a pasted log tail; '-' reads stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)  # argparse exits 2 on a usage error
    if args.fetch_timeout is not None and not 0 < args.fetch_timeout <= FETCH_MAX_S:
        print(f"doctrine-recall: --fetch-timeout must be > 0 and <= {FETCH_MAX_S:g} (use --no-fetch to skip)", file=sys.stderr)
        return 2
    if args.fold_debt:
        if not args.since or args.query or args.git_timeout <= 0:
            print("doctrine-recall: --fold-debt needs --since SHA, --git-timeout > 0 and no query", file=sys.stderr)
            return 2
    else:
        if args.since:
            print("doctrine-recall: --since only applies with --fold-debt", file=sys.stderr)
            return 2
        query = sys.stdin.read() if args.query == ["-"] else " ".join(args.query)
        if not query.strip() or args.top < 1:
            print("doctrine-recall: need a non-empty query and --top >= 1", file=sys.stderr)
            return 2
    bus = resolve_bus(args.bus)
    if not bus.is_dir():
        print(f"doctrine-recall: bus not found: {bus} (pass --bus PATH)", file=sys.stderr)
        return 2
    if args.fold_debt:
        fetch_s = 0.0 if args.no_fetch else args.fetch_timeout or FETCH_BUDGET_FOLD_DEBT_S
        result, code, message = fold_debt(bus, args.since, own_project(), args.git_timeout, fetch_s)
        if result is None:
            print(f"doctrine-recall: fold-debt: {message}", file=sys.stderr)
            return code
        if result["fetch"].startswith("FETCH_FAILED"):
            print(f"{result['fetch']} (priced against HEAD only)", file=sys.stderr)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    memory = Path(args.project_memory) if args.project_memory else REPO_ROOT / ".claude-state" / "project-memory"
    fetch_s = 0.0 if args.no_fetch else args.fetch_timeout or FETCH_BUDGET_SEARCH_S
    try:
        result = search(
            query, bus, memory if memory.is_dir() else None, args.top, blame_budget_s=args.blame_timeout,
            sync=sync_bus(bus, fetch_s),
        )
    except CorpusUnavailable:
        found = [name for name in BUS_FILES if (bus / name).is_file()]
        found += [f"specs/{path.parent.name}/cards.md" for path in card_files(bus, own_project())]
        reason = "no TRAPS.md/RECEIPTS.md/RULINGS.md" if not found else f"no entries parsed from {', '.join(found)}"
        print(f"doctrine-recall: corpus unavailable at {bus} ({reason})", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
