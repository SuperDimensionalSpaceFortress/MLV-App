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
from datetime import date, datetime, timedelta
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

Only rows with `kind` of `product` or `playback` are shown. Hub-tooling, factory,
hygiene and ci-flake cards are tooling churn and are not counted here. Rows the board has
not classified are not counted either, even when the card name says PLAYBACK.
"""

# Standing owner ruling (chat e7cff0a2, 2026-10-25T00:44:41Z); static text, so a re-render keeps it.
MACOS_SCOPE_NOTE = (
    "**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and "
    "GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, "
    "GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI "
    "path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the "
    "compile canary, PR #671)."
)


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
    return max(stamps)


def _sort_key(r: dict) -> tuple:
    return (r["priority"], r["source_ledger_stamp"], r["card"])


def _chain(r: dict) -> str:
    oc = r.get("order_chain")
    if not oc:
        return "-"
    pred = oc.get("predecessor")
    return "%s #%s %s" % (oc["chain"], oc["index"], "after " + pred if pred else "first")


def _note(r: dict) -> str:
    text = " ".join((r.get("notes") or "").split())
    if not text:
        return "(no note)"
    return text if len(text) <= NOTE_MAX else text[: NOTE_MAX - 3] + "..."


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
    """(open_rows, parked_rows, days, landed_by_day, queued_by_day) for product/playback rows."""
    rows = [r for r in data["rows"] if r["kind"] in KINDS]
    open_rows = sorted((r for r in rows if r["state"] not in CLOSED_STATES), key=_sort_key)
    parked = sorted((r for r in rows if r["state"] == "parked"), key=_sort_key)
    last = _day(data["generated_utc"])
    days = [last - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]
    landed = {d: 0 for d in days}
    queued = {d: 0 for d in days}
    for r in rows:
        d = _day(r["source_ledger_stamp"])
        if d in queued:
            queued[d] += 1
        if r["state"] == "merged":
            d = _day(_landed_stamp(r))
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

    out = [HEADER, "generated_from: %s" % generated, "", "## Open product and playback cards", "",
           MACOS_SCOPE_NOTE, ""]
    out.append("| P | card | kind | state | queued since | order_chain |")
    out.append("|---|---|---|---|---|---|")
    for r in open_rows:
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            r["priority"], r["card"], r["kind"], r["state"], r["source_ledger_stamp"][:10], _chain(r)))
    if not open_rows:
        out.append("| - | (none open) | - | - | - | - |")

    out += ["", "## Drain", "", "product+playback open: %d" % len(open_rows),
            "", "Window: the %d UTC days ending %s (the last day is partial)." % (WINDOW_DAYS, last.isoformat()),
            "Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp "
            "(approximate: a lower bound on the merge time, not a measurement); "
            "queued = rows of any state by `source_ledger_stamp`.", "",
            "| day (UTC) | landed | queued |", "|---|---|---|"]
    for d in days:
        out.append("| %s | %d | %d |" % (d.isoformat(), landed[d], queued[d]))
    out.append("| total | %d | %d |" % (landed_total, queued_total))
    out += ["", _drain_line(len(open_rows), landed_total, queued_total), "",
            "## Parked with reason", "", MACOS_SCOPE_NOTE, ""]
    for r in parked:
        out.append("- %s (P%s): %s" % (r["card"], r["priority"], _note(r)))
    if not parked:
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
    for key in ("evidence", "notes"):
        if r.get(key) is not None and not isinstance(r[key], str):
            _die("%s has a non-string %s" % (where, key))
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
