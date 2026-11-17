"""Render docs/roadmap.md from the board's card-queue.json.

The roadmap used to mirror the frozen dual-lane queue.json, 14 of whose 16 rows have
landed, so it read as drained while the live dispatch surface held dozens of open cards.
card-queue.json (schema mlv-app/card-queue/v1) is the live surface: this renders its open
`product`/`playback` rows and the rate at which they are landing versus arriving.

The card-queue file is board-local (gitignored) and absent from hosted CI, so the path is
a required argument. The output is a pure function of that file: no wall clock, the
7-day window is anchored on its `generated_utc`, so `--check` is stable.

Landing time of a merged row is its `merged_utc` when the row carries one. Without it,
the landing time is the newest of `source_ledger_stamp`, `pinned_utc` and every
`declared@<utc>` in `evidence`, which is the last time the hub touched the row. Declaration
and pin stamps are not merge events, so that fallback is an approximation (a lower bound
on the real merge time), not a measurement. Intake time is `source_ledger_stamp`.

Scope rulings (for example a deferral) are data, not code: an optional top-level `scope_notes`
list of `{"title": ..., "text": ...}` in card-queue.json is rendered as `**title.** text` under
the open table and under "Parked with reason". This file holds no PR number or card id; a queue
without `scope_notes` renders no note. The hub owns the block and removes a note when it lapses.

Owner asks (OWNER-ASK-PARK-GUARD-1): a row with `origin` of `owner` is counted whatever its
`kind` (an owner ask filed as unclassified or hub-tooling used to vanish from this file). On
the open table its kind cell reads `<kind> (owner)` when the kind is not product or playback.
Parked owner asks get their own `## Parked owner asks` section, longest-parked first, with the
park reason and the age in whole days from the park stamp (`pinned_utc` when present, else
`source_ledger_stamp`) to `generated_utc`; they are not repeated under "Parked with reason".
At most OWNER_LIST_MAX (25) are listed, then one `- ... and N more owner asks parked` line, and
the open table lists at most 25 owner asks of another kind the same way, so docs/roadmap.md
stays inside its 20000-byte budget however many owner asks the board holds.
Deny by default (r2): a parked owner ask is OWED unless its row carries a structured
`resolution` (one of done, duplicate, superseded, no_go, refused), which only
`Update-CardQueue.ps1 -Set <CARD> -State parked -Resolution <r>` writes. The park note is
never read for that: free text that says "shipped" or "DONE" is still owed. A resolved owner
ask of product or playback kind is listed under "Parked with reason" with its resolution; one
of another kind is tooling churn and is counted on a single line there. Any other
`resolution` value is bad input (exit 2).

Usage:
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --out docs/roadmap.md
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --out docs/roadmap.md --check
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --drain-line

Exit: 0 ok / written, 1 `--check` and the file differs (or is missing), 2 bad input.
Bad input includes a malformed stamp (`generated_utc`, `source_ledger_stamp`, `pinned_utc`,
`merged_utc`, a `declared@` token in `evidence`) or a malformed `order_chain`; the message
names the file, the row index and the field. A `declared@` token is malformed unless it is
exactly `declared@YYYY-MM-DDTHH:MM:SSZ` (trailing `.,;:)]}'"` is ignored): a non-zero-padded
stamp would otherwise lose or win the string `max()` that picks the landing day.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import calendar
from datetime import date, datetime, timedelta
from fractions import Fraction   # exact instants: a rational number has no precision context to round in
import math
from pathlib import Path

SCHEMA = "mlv-app/card-queue/v1"
KINDS = ("product", "playback")
CLOSED_STATES = ("merged", "parked")
WINDOW_DAYS = 7
NOTE_MAX = 140
REQUIRED = ("card", "kind", "priority", "state", "source_ledger_stamp")
DECLARED_RX = re.compile(r"declared@(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
# Every declared@ token, however it is spelled; validation compares it to DECLARED_RX.
DECLARED_TOKEN_RX = re.compile(r"declared@(\S*)")
TOKEN_TRAILING_PUNCT = ".,;:)]}'\""

HEADER = """\
# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`
(schema `mlv-app/card-queue/v1`), the live dispatch surface. It replaces the earlier
mirror of the frozen dual-lane `queue.json`, whose 16 rows had mostly landed. The
card-queue file lives under `.claude-state/hub-autopilot/`, which is gitignored and absent
from every hosted checkout, so this file is the only place a reviewer without local state
can see the open product backlog. Where the two disagree, `card-queue.json` wins:
re-render, do not edit by hand. The hub checks freshness with `--check`.

Only rows with `kind` of `product` or `playback` are shown, plus owner asks
(`origin` = `owner`) of any kind. Other hub-tooling, factory, hygiene and ci-flake cards are
tooling churn and are not counted here. Rows the board has not classified are not counted
either, even when the card name says PLAYBACK, unless the owner asked for them.
"""
OWNER = "owner"


def _counted(r: dict) -> bool:
    """A product/playback row, or an owner ask of any kind."""
    return r["kind"] in KINDS or r.get("origin") == OWNER


def _kind_cell(r: dict) -> str:
    return r["kind"] if r["kind"] in KINDS else "%s (owner)" % r["kind"]


def _parked_since(r: dict) -> str:
    return r.get("pinned_utc") or r["source_ledger_stamp"]


# Deny by default (OWNER-ASK-PARK-GUARD-1 r2, adversary A1 on 8ba4f8ab; fleet card
# mlv-app/spelling-chase-scanner-needs-a-deny-by-default-name, softwarefactory-fleet-doctrine specs/mlv-app/cards.md:663).
# r1 read the park NOTE for completion words and then chased each way prose can say "not yet": "after #123 has shipped",
# "hasn't shipped", "blocker #123 shipped; still waiting for #456", "not\nDONE". Every new rule leaked to a new
# spelling, which is the trap that card records. So the note is never read here. A parked owner ask is owed unless its
# row carries a structured `resolution` from the closed vocabulary below, and only the hub writes one, through
# Update-CardQueue.ps1 -Set <CARD> -State parked -Resolution <r> (a value outside the vocabulary exits 2 there and here).
# The price is over-reporting: a delivered ask stays on the owed list until the hub states its resolution once.
RESOLUTIONS = ("done", "duplicate", "superseded", "no_go", "refused")


def _resolution(r: dict) -> str:
    """The row's structured resolution, or '' when it has none (absent or null): that row is not closed."""
    return r.get("resolution") or ""


def _undelivered_owner_ask(r: dict) -> bool:
    """An owner ask still owed: origin owner and no resolution. Its notes are display text only."""
    return r.get("origin") == OWNER and not _resolution(r)


# At most this many owner asks are listed per section (longest-parked first for the parked ones, table order for the
# open ones); the rest are counted on one line. docs/roadmap.md has a 20000-byte budget (test_roadmap_queue_parity.py).
OWNER_LIST_MAX = 25


# (adversary pass 2 A3/A4/A6/A7) a stamp is read exactly: every fractional digit is kept, an explicit offset is applied, and the
# instant is a Fraction count of UTC seconds, so a 7-digit fraction, a -02:00 offset or a far-future year never truncates,
# rounds or overflows. A stamp in no such shape (date only, an odd tail) reads as that date's UTC midnight, as before.
# (r2 A2 on 8ba4f8ab) the fraction is digits / 10**len(digits), an exact rational: no arithmetic context caps the digits, so
# 2026-10-22T00:00:00.0000000000000000001Z is later than 00:00:00Z by exactly 1e-19 s and never rounds into the next whole second.
_STAMP_RX = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d+))?)?\s*(Z|[+-]\d\d:?\d\d)?$", re.IGNORECASE)


def _instant(stamp: str) -> Fraction:
    """Exact UTC seconds since the epoch, as a Fraction (every fractional digit kept)."""
    m = _STAMP_RX.match(stamp.strip())
    if not m:
        d = _day(stamp)
        return Fraction(calendar.timegm((d.year, d.month, d.day, 0, 0, 0)))
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    secs = calendar.timegm((y, mo, d, h, mi, int(m.group(6) or 0)))
    tz = m.group(8)
    if tz and tz.upper() != "Z":
        sign = -1 if tz[0] == "-" else 1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        secs -= sign * (hh * 3600 + mm * 60)
    digits = m.group(7) or ""
    return Fraction(secs) + (Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0))


def _utc_day(stamp: str) -> date:
    """The UTC calendar day of a stamp (an offset can move it off the date its text starts with)."""
    whole = math.floor(_instant(stamp))   # exact floor of a rational: 23:59:59.999.. stays on its own day
    try:
        return date(1970, 1, 1) + timedelta(seconds=whole)
    except OverflowError:
        return _day(stamp)


def _die(message: str) -> "None":
    print("render_roadmap: " + message, file=sys.stderr)
    raise SystemExit(2)


def _day(stamp: str) -> date:
    return datetime.strptime(stamp[:10], "%Y-%m-%d").date()


def _landed_stamp(r: dict) -> str:
    if r.get("merged_utc"):
        return r["merged_utc"]
    stamps = [r["source_ledger_stamp"]]
    if r.get("pinned_utc"):
        stamps.append(r["pinned_utc"])
    stamps += DECLARED_RX.findall(r.get("evidence") or "")
    return max(stamps, key=_instant)   # newest INSTANT: a string max picks the older of 08:00:00Z and 08:00:00.100Z


def _sort_key(r: dict) -> tuple:
    return (r["priority"], _instant(r["source_ledger_stamp"]), r["card"])


def _chain(r: dict) -> str:
    oc = r.get("order_chain")
    if not oc:
        return "-"
    pred = oc.get("predecessor")
    return "%s #%s %s" % (oc["chain"], oc["index"], "after " + pred if pred else "first")


def _scope_notes(data: dict) -> list:
    """Scope-note paragraphs (each followed by a blank line) from the queue's optional scope_notes block."""
    out = []
    for n in data.get("scope_notes") or []:
        out += ["**%s.** %s" % (n["title"], n["text"]), ""]
    return out


def _note(r: dict, limit: int = NOTE_MAX) -> str:
    text = " ".join((r.get("notes") or "").split())
    if not text:
        return "(no note)"
    return text if len(text) <= limit else text[: limit - 3] + "..."


# An owner-ask line already carries kind and age, so its park reason is cut shorter (docs/roadmap.md has a 20000-byte budget).
OWNER_NOTE_MAX = 100


def _drain_line(open_n: int, landed: int, queued: int) -> str:
    if open_n == 0:
        return "product is drained (0 open)"
    if queued >= landed:
        return "does not drain (queued >= landed)"
    # net per day = (landed - queued) / WINDOW_DAYS; days = ceil(open / net), in integers.
    net = landed - queued
    days = -(-open_n * WINDOW_DAYS // net)
    return "at the 7-day mean, product drains in %d days" % days


def _stats(data: dict) -> tuple:
    """(open_rows, parked_rows, days, landed_by_day, queued_by_day) for product/playback rows and owner asks."""
    rows = [r for r in data["rows"] if _counted(r)]
    open_rows = sorted((r for r in rows if r["state"] not in CLOSED_STATES), key=_sort_key)
    parked = sorted((r for r in rows if r["state"] == "parked"), key=_sort_key)
    last = _utc_day(data["generated_utc"])
    days = [last - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]
    landed = {d: 0 for d in days}
    queued = {d: 0 for d in days}
    for r in rows:
        d = _utc_day(r["source_ledger_stamp"])
        if d in queued:
            queued[d] += 1
        if r["state"] == "merged":
            d = _utc_day(_landed_stamp(r))
            if d in landed:
                landed[d] += 1
    return open_rows, parked, days, landed, queued


def drain_line(data: dict) -> str:
    """One line for the board snapshot (inflight.md); no trailing newline."""
    open_rows, _parked, _days, landed, queued = _stats(data)
    landed_total, queued_total = sum(landed.values()), sum(queued.values())
    return "Product drain: %d open, %d landed vs %d queued over %d UTC days (card-queue %s): %s" % (
        len(open_rows), landed_total, queued_total, WINDOW_DAYS, data["generated_utc"],
        _drain_line(len(open_rows), landed_total, queued_total))


def render(data: dict) -> str:
    open_rows, parked, days, landed, queued = _stats(data)
    generated = data["generated_utc"]
    last = days[-1]
    landed_total, queued_total = sum(landed.values()), sum(queued.values())

    scope = _scope_notes(data)
    out = [HEADER, "generated_from: %s" % generated, "", "## Open product and playback cards", "", *scope]
    out.append("| P | card | kind | state | queued since | order_chain |")
    out.append("|---|---|---|---|---|---|")
    other_kind_seen = 0
    for r in open_rows:
        if r["kind"] not in KINDS:   # an owner ask of another kind (r2 A3 sibling): the first OWNER_LIST_MAX in table order are listed
            other_kind_seen += 1
            if other_kind_seen > OWNER_LIST_MAX:
                continue
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            r["priority"], r["card"], _kind_cell(r), r["state"], _utc_day(r["source_ledger_stamp"]).isoformat(), _chain(r)))
    if not open_rows:
        out.append("| - | (none open) | - | - | - | - |")
    other_kind_asks = sum(1 for r in open_rows if r["kind"] not in KINDS)
    if other_kind_asks > OWNER_LIST_MAX:   # no leading digit cell, so the parity check never reads it as a card row
        out.append("| - | ... and %d more open owner asks of another kind (card-queue.json) | - | - | - | - |" % (
            other_kind_asks - OWNER_LIST_MAX))
    open_line = "product+playback open: %d" % len(open_rows)
    if other_kind_asks:
        open_line += " (incl. %d owner ask%s of another kind)" % (other_kind_asks, "" if other_kind_asks == 1 else "s")

    out += ["", "## Drain", "", open_line,
            "", "Window: the %d UTC days ending %s (the last day is partial)." % (WINDOW_DAYS, last.isoformat()),
            "Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp "
            "(approximate: a lower bound on the merge time, not a measurement); "
            "queued = rows of any state by `source_ledger_stamp`.", "",
            "| day (UTC) | landed | queued |", "|---|---|---|"]
    for d in days:
        out.append("| %s | %d | %d |" % (d.isoformat(), landed[d], queued[d]))
    out.append("| total | %d | %d |" % (landed_total, queued_total))
    owner_parked = sorted((r for r in parked if _undelivered_owner_ask(r)),
                          key=lambda r: (_instant(_parked_since(r)), r["priority"], r["card"]))
    generated_at = _instant(generated)
    other_parked = [r for r in parked if not _undelivered_owner_ask(r)]
    out += ["", _drain_line(len(open_rows), landed_total, queued_total), "",
            "## Parked owner asks", ""]
    for r in owner_parked[:OWNER_LIST_MAX]:   # r2 A3: the longest-parked OWNER_LIST_MAX, then one count line
        since = _parked_since(r)
        age = max(0, math.floor((generated_at - _instant(since)) / 86400))   # elapsed whole days between exact instants, not a date difference
        out.append("- %s (P%s, %s): parked %d d (since %s): %s" % (
            r["card"], r["priority"], r["kind"], age, _utc_day(since).isoformat(), _note(r, OWNER_NOTE_MAX)))
    if len(owner_parked) > OWNER_LIST_MAX:
        out.append("- ... and %d more owner asks parked (card-queue.json)" % (len(owner_parked) - OWNER_LIST_MAX))
    if not owner_parked:
        out.append("- (no owner ask is parked)")
    out += ["", "## Parked with reason", "", *scope]
    # every owner row here carries a resolution (an unresolved one is owed, above). One of another kind is tooling churn: counted, not listed.
    closed_other = [r for r in other_parked if r.get("origin") == OWNER and r["kind"] not in KINDS]
    for r in other_parked:
        if r.get("origin") == OWNER:
            if r["kind"] in KINDS:
                out.append("- %s (P%s, owner ask, %s): %s" % (r["card"], r["priority"], _resolution(r), _note(r)))
            continue
        out.append("- %s (P%s): %s" % (r["card"], r["priority"], _note(r)))
    if closed_other:
        counts = ["%s %d" % (v, n) for v in RESOLUTIONS for n in [sum(1 for r in closed_other if _resolution(r) == v)] if n]
        out.append("- %d owner ask%s of another kind %s closed with a resolution (%s; card-queue.json)" % (
            len(closed_other), "" if len(closed_other) == 1 else "s", "is" if len(closed_other) == 1 else "are", ", ".join(counts)))
    if not other_parked:
        out.append("- (none parked)")
    return "\n".join(out) + "\n"


def _valid_stamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _day(value)
    except ValueError:
        return False
    return True


def _check_row(path: Path, i: int, r: dict) -> None:
    where = "%s row %d (%s)" % (path, i, r["card"])
    for key in ("source_ledger_stamp", "pinned_utc", "merged_utc"):
        if r.get(key) is not None and not _valid_stamp(r[key]):
            _die("%s has a malformed %s: %r (want YYYY-MM-DD...)" % (where, key, r[key]))
    for key in ("evidence", "notes", "origin"):
        if r.get(key) is not None and not isinstance(r[key], str):
            _die("%s has a non-string %s" % (where, key))
    res = r.get("resolution")   # r2 A1: a closed vocabulary; null is the same as absent (no resolution, so an owner ask stays owed)
    if res is not None and not (isinstance(res, str) and res in RESOLUTIONS):
        _die("%s has a bad resolution: %r (want one of %s, or none)" % (where, res, "|".join(RESOLUTIONS)))
    for token in DECLARED_TOKEN_RX.findall(r.get("evidence") or ""):
        token = token.rstrip(TOKEN_TRAILING_PUNCT)
        stamp = DECLARED_RX.fullmatch("declared@" + token)
        if not stamp or not _valid_stamp(stamp.group(1)):
            _die("%s has a malformed declared@ stamp in evidence: 'declared@%s' "
                 "(want declared@YYYY-MM-DDTHH:MM:SSZ)" % (where, token))
    oc = r.get("order_chain")
    if oc is None:
        return
    if not isinstance(oc, dict):
        _die("%s has an order_chain that is not an object: %r" % (where, oc))
    if not isinstance(oc.get("chain"), str):
        _die("%s has an order_chain without a string chain" % where)
    if not isinstance(oc.get("index"), int) or isinstance(oc["index"], bool):
        _die("%s has an order_chain without an integer index" % where)
    if oc.get("predecessor") is not None and not isinstance(oc["predecessor"], str):
        _die("%s has an order_chain with a non-string predecessor" % where)


def _check_scope_notes(path: Path, notes: object) -> None:
    if notes is None:
        return
    if not isinstance(notes, list):
        _die("%s has a scope_notes that is not a list: %r" % (path, notes))
    for i, n in enumerate(notes):
        if not isinstance(n, dict) or not all(isinstance(n.get(k), str) and n[k].strip() for k in ("title", "text")):
            _die("%s scope_notes entry %d needs a non-empty string title and text: %r" % (path, i, n))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _die("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("rows"), list):
        _die("%s is not a %s file" % (path, SCHEMA))
    if not isinstance(data.get("generated_utc"), str):
        _die("%s has no generated_utc" % path)
    if not _valid_stamp(data["generated_utc"]):
        _die("%s has a malformed generated_utc: %r (want YYYY-MM-DD...)" % (path, data["generated_utc"]))
    _check_scope_notes(path, data.get("scope_notes"))
    for i, r in enumerate(data["rows"]):
        missing = [k for k in REQUIRED if not isinstance(r, dict) or r.get(k) is None]
        if missing:
            _die("%s row %d lacks %s" % (path, i, ", ".join(missing)))
        if not isinstance(r["priority"], int):
            _die("%s row %d (%s) has a non-integer priority" % (path, i, r["card"]))
        _check_row(path, i, r)
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render docs/roadmap.md from card-queue.json.")
    ap.add_argument("--card-queue", required=True, type=Path, help="board-local card-queue.json")
    ap.add_argument("--out", type=Path, help="the roadmap file to write or check (required unless --drain-line)")
    ap.add_argument("--check", action="store_true", help="exit 1 when --out differs; write nothing")
    ap.add_argument("--drain-line", action="store_true",
                    help="print the one-line drain summary for the board snapshot; write nothing")
    args = ap.parse_args(argv)

    data = _load(args.card_queue)
    if args.drain_line:
        print(drain_line(data))
        return 0
    if args.out is None:
        ap.error("--out is required unless --drain-line")
    text = render(data)
    if args.check:
        try:
            on_disk = args.out.read_bytes().decode("utf-8").replace("\r\n", "\n")
        except OSError:
            print("render_roadmap: %s is missing" % args.out, file=sys.stderr)
            return 1
        if on_disk != text:
            print("render_roadmap: %s is stale; re-run without --check" % args.out, file=sys.stderr)
            return 1
        return 0
    args.out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
