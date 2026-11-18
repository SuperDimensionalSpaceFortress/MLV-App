"""HEAVY-SLOT-SECOND-SLOT-MEASURED-1: tools/coordination/heavy-slot-ab/ (reader module, driver, evaluator).

PC-MINI3 runs one heavy build slot while its CPU, RAM and (tight) thermals leave room for a second. The card measures a 2-slot setting
in a bounded A/B window in which ONLY the fast-suites pytest ticket takes a 2-slot set (builds stay at count 1). The driver is the only
writer of the switch file; the reader answers 2 only while a live driver says state B. Everything here runs through seams (a scratch
HeavySlot dir, a scratch feed / memory / disk / event probe, a scratch switch and a per-test mutex name): nothing touches the machine
heavy slot, C:\\mlvtmp\\locks or the real feed, and no test opens a heavy-slot mutex.
"""

import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AB_DIR = REPO_ROOT / "tools" / "coordination" / "heavy-slot-ab"
MODULE = AB_DIR / "HeavySlotAb.psm1"
DRIVER = AB_DIR / "Invoke-HeavySlotAbDriver.ps1"
EVALUATOR = AB_DIR / "Measure-HeavySlotAb.ps1"
PWSH = shutil.which("pwsh.exe") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(os.name != "nt" or not PWSH, reason="the heavy-slot A/B tools are Windows PowerShell 7 tools")

POLL = 0.3
UTC = timezone.utc


def _ps(args, timeout=120):
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def iso(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------------------------------------------------------
# this very process, as a "live driver" for hand-written switch files and "live holders" for held records
# ---------------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def me():
    out = _ps(["-Command", f"$p = Get-Process -Id {os.getpid()}; $t = $p.StartTime.ToUniversalTime(); \"$($t.Ticks)|$($t.ToString('o'))\""])
    ticks, start_iso = out.stdout.strip().split("|")
    dead = _ps(["-Command", "$PID"]).stdout.strip()
    return {"pid": os.getpid(), "ticks": int(ticks), "iso": start_iso, "dead_pid": int(dead)}


def switch_doc(me, **over):
    now = datetime.now(UTC)
    doc = {
        "schema": "heavy-slot-ab.v1", "count": 2, "state": "B",
        "windowStartUtc": iso(now - timedelta(minutes=5)), "endUtc": iso(now + timedelta(hours=1)),
        "driverPid": me["pid"], "driverStartTicks": me["ticks"],
    }
    doc.update(over)
    return doc


# ---------------------------------------------------------------------------------------------------------------------------
# 1. the reader
# ---------------------------------------------------------------------------------------------------------------------------
def _read_counts(tmp_path: Path, files: dict) -> dict:
    script = tmp_path / "read-counts.ps1"
    lines = ["$ErrorActionPreference = 'Stop'", f"Import-Module -Force '{MODULE}'"]
    for name, path in files.items():
        lines.append(f"Write-Output ('{name}=' + (Get-HeavySlotAbCount -SwitchPath '{path}'))")
    script.write_text("\n".join(lines) + "\n", encoding="ascii")
    out = _ps(["-File", str(script)])
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in out.stdout.split() if "=" in line)


def test_reader_returns_2_only_for_a_valid_live_b_window(tmp_path, me):
    now = datetime.now(UTC)
    cases = {
        "valid-b": (switch_doc(me), 2),
        "exactly-25h": (switch_doc(me, windowStartUtc=iso(now - timedelta(hours=1)), endUtc=iso(now + timedelta(hours=24))), 2),
        "state-a": (switch_doc(me, state="A", count=1), 1),
        "state-a-count-2": (switch_doc(me, state="A"), 1),
        "reverted": (switch_doc(me, state="REVERTED", count=1), 1),
        "count-3": (switch_doc(me, count=3), 1),
        "count-1-state-b": (switch_doc(me, count=1), 1),
        "count-as-string": (switch_doc(me, count="2"), 1),
        "wrong-schema": (switch_doc(me, schema="heavy-slot-ab.v2"), 1),
        "lowercase-state": (switch_doc(me, state="b"), 1),
        "expired": (switch_doc(me, windowStartUtc=iso(now - timedelta(hours=2)), endUtc=iso(now - timedelta(seconds=5))), 1),
        "window-over-25h": (switch_doc(me, windowStartUtc=iso(now - timedelta(hours=1)), endUtc=iso(now + timedelta(hours=24, minutes=1))), 1),
        "end-before-start": (switch_doc(me, windowStartUtc=iso(now + timedelta(hours=2)), endUtc=iso(now + timedelta(hours=1))), 1),
        "driver-dead": (switch_doc(me, driverPid=me["dead_pid"]), 1),
        "pid-reuse": (switch_doc(me, driverStartTicks=me["ticks"] + 10_000_000), 1),
        "no-driver-pid": (switch_doc(me, driverPid=0), 1),
    }
    files = {}
    for name, (doc, _want) in cases.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(doc), encoding="ascii")
        files[name] = path
    files["missing"] = tmp_path / "no-such-switch.json"
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="ascii")
    files["malformed"] = malformed
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="ascii")
    files["empty"] = empty
    got = _read_counts(tmp_path, files)
    want = {name: str(w) for name, (_d, w) in cases.items()}
    want.update({"missing": "1", "malformed": "1", "empty": "1"})
    assert got == want


def test_the_reader_region_is_one_self_contained_block_for_the_hub_patch():
    text = MODULE.read_text(encoding="ascii")
    assert len(re.findall(r"(?m)^# HEAVY_SLOT_AB_READER v1 BEGIN\s*$", text)) == 1
    assert len(re.findall(r"(?m)^# HEAVY_SLOT_AB_READER v1 END\s*$", text)) == 1
    region = re.search(r"(?s)# HEAVY_SLOT_AB_READER v1 BEGIN(.*?)# HEAVY_SLOT_AB_READER v1 END", text).group(1)
    assert "Import-Module" not in region and "$PSScriptRoot" not in region, "the inlined copy must need nothing from a worktree"
    assert "Get-Process -Id" in region and "Win32_" not in region


# ---------------------------------------------------------------------------------------------------------------------------
# the driver world: scratch HeavySlot dir, feed with a live writer, probe files, switch, window dir, mutex name
# ---------------------------------------------------------------------------------------------------------------------------
FEED_HEADER = "Date,Time,Total CPU Usage [%],CPU Package [C],CPU Package Power [W],Core Thermal Throttling (avg) [Yes/No],Package/Ring Thermal Throttling [Yes/No]\n"


class Feed:
    """The CoreTemp feed: a row every 0.2 s stamped with LOCAL time, like the real one. temp / pause are the test's levers."""

    def __init__(self, path: Path, temp=60.0):
        self.path = path
        self.temp = temp
        self.paused = False
        self._stop = threading.Event()
        path.write_text(FEED_HEADER, encoding="ascii")
        self._row()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _row(self):
        now = datetime.now()
        stamp = now.strftime("%d.%m.%Y,%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        with open(self.path, "a", encoding="ascii") as fh:
            fh.write(f"{stamp},20.0,{self.temp},40.0,No,No\n")

    def _run(self):
        while not self._stop.wait(0.2):
            if not self.paused:
                try:
                    self._row()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)


class World:
    def __init__(self, root: Path, name: str, me: dict, marker=True, pin_ok=True, feed_fresh=True):
        self.root = root / name
        self.root.mkdir(parents=True)
        self.me = me
        self.hs = self.root / "hs"
        (self.hs / "bin").mkdir(parents=True)
        (self.hs / "held").mkdir()
        (self.hs / "receipts").mkdir()
        (self.hs / "ledger.jsonl").write_text("", encoding="ascii")
        body = b"# fake heavy-slot.ps1 for test_heavy_slot_ab\n"
        (self.hs / "bin" / "heavy-slot.ps1").write_bytes(body)
        self.pin = sha256(body).hexdigest() if pin_ok else "0" * 64
        self.fast = self.root / "fast-suites-for-diff.ps1"
        self.fast.write_text("# HEAVY_SLOT_AB_READER v1\n" if marker else "# nothing here\n", encoding="ascii")
        self.feed_path = self.root / "feed.csv"
        self.feed = Feed(self.feed_path)
        if not feed_fresh:
            self.feed.close()
            old = datetime.now() - timedelta(minutes=10)
            self.feed_path.write_text(FEED_HEADER + old.strftime("%d.%m.%Y,%H:%M:%S.000") + ",20.0,60.0,40.0,No,No\n", encoding="ascii")
        self.memory = self.root / "memory.json"
        self.set_memory(70.0, 60.0)
        self.disk = self.root / "disk.json"
        self.set_disk(500.0)
        self.event = self.root / "event.txt"
        self.event.write_text("0", encoding="ascii")
        self.switch = self.root / "switch.json"
        self.window = self.root / "window"
        self.mutex = "Global\\MLV-HeavySlotAbDriver-test-" + uuid.uuid4().hex[:12]
        self.stdout = self.root / "driver.out.txt"
        self.stderr = self.root / "driver.err.txt"

    def set_memory(self, commit, phys):
        self.memory.write_text(json.dumps({"freeCommitGB": commit, "freePhysGB": phys}), encoding="ascii")

    def set_disk(self, gib):
        self.disk.write_text(json.dumps({"freeGiB": gib}), encoding="ascii")

    def add_holder(self, ticket, slot, cls="shared", whole=False):
        rec = {"ticket": ticket, "label": f"mlv:TEST:{'build' if slot == 0 else 'pytest'}", "class": cls, "wholeMachine": whole,
               "slotName": "Global\\HeavySlot", "slotCount": 2, "slots": [slot], "pid": self.me["pid"], "pidStart": self.me["iso"], "phase": "running"}
        (self.hs / "held" / f"{ticket}.json").write_text(json.dumps(rec), encoding="ascii")

    def append_ledger(self, row: dict):
        with open(self.hs / "ledger.jsonl", "a", encoding="ascii") as fh:
            fh.write(json.dumps(row) + "\n")

    def args(self, hours, block_minutes, extra=()):
        return [
            PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(DRIVER),
            "-WindowDir", str(self.window), "-Hours", str(hours), "-BlockMinutes", str(block_minutes), "-PollSeconds", str(POLL),
            "-SwitchPath", str(self.switch), "-HeavySlotDir", str(self.hs), "-CoreTempFeed", str(self.feed_path),
            "-MemoryProbeFile", str(self.memory), "-EventProbeFile", str(self.event), "-DiskProbeFile", str(self.disk),
            "-FastSuitesPath", str(self.fast), "-PinSha", self.pin, "-MutexName", self.mutex, "-NoPerfProbe", *extra,
        ]

    def start(self, hours, block_minutes, extra=()):
        out = open(self.stdout, "ab")
        err = open(self.stderr, "ab")
        return subprocess.Popen(self.args(hours, block_minutes, extra), stdout=out, stderr=err)

    def text(self):
        return self.stdout.read_text(errors="replace") + self.stderr.read_text(errors="replace")

    def switch_doc(self):
        try:
            return json.loads(self.switch.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def samples(self):
        path = self.window / "ab-samples.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def blocks(self):
        path = self.window / "ab-blocks.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []

    def receipt(self):
        path = self.window / "ab-receipt.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def close(self):
        self.feed.close()


def _wait(pred, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return None


def _reader_counts(tmp: Path, switch: Path) -> int:
    return int(_read_counts(tmp, {"x": switch})["x"])


# ---------------------------------------------------------------------------------------------------------------------------
# 2. hard aborts: each reverts the switch to count 1 within a few polls and the receipt names the reason
# ---------------------------------------------------------------------------------------------------------------------------
def _hold_a_ticket(w: World, name="t1"):
    w.add_holder(name, 0)


def _receipt_unreadable_then_valid(w):
    path = w.hs / "receipts" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "998Z-2-fedcba.json")
    path.write_text('{"ticket": "x4", "class": "shar', encoding="ascii")   # half written
    threading.Timer(1.0, lambda: path.write_text(json.dumps({"ticket": "x4", "class": "shared", "wholeMachine": True}), encoding="ascii")).start()


ABORTS = {
    # name: (driver extra args, setup(w), flip(w) -> condition delay seconds, expected reason prefix, max samples after the flip)
    "thermal-cap": (["-CapSeconds", "0.3", "-CapWindowSeconds", "10"], None, lambda w: setattr(w.feed, "temp", 96.0), 0.0, "thermal-cap", 5),
    "warm-while-2": (["-WarmWindowSeconds", "2", "-WarmFraction", "0.3"], lambda w: (w.add_holder("h0", 0), w.add_holder("h1", 1)),
                     lambda w: setattr(w.feed, "temp", 91.0), 0.0, "warm-while-2", 6),
    "stale-feed": (["-FeedAbortSeconds", "1.0"], None, lambda w: setattr(w.feed, "paused", True), 1.0, "stale-feed", 8),
    "low-memory": ([], None, lambda w: w.set_memory(5.0, 60.0), 0.0, "low-memory", 4),
    "event-2004": ([], None, lambda w: w.event.write_text("1", encoding="ascii"), 0.0, "event-2004", 3),
    "low-disk": ([], None, lambda w: w.set_disk(10.0), 0.0, "low-disk", 3),
    "unguarded": ([], None, lambda w: w.append_ledger({"at": iso(datetime.now(UTC)), "event": "unguarded", "ticket": "u1", "label": "mlv:X:build", "reason": "t"}), 0.0, "unguarded", 3),
    "exclusive-admit-ledger": ([], None, lambda w: w.append_ledger({"at": iso(datetime.now(UTC)), "event": "admit", "ticket": "x1", "label": "mlv:X:build", "class": "exclusive"}), 0.0, "exclusive-admit", 3),
    "exclusive-admit-receipt": ([], None, lambda w: (w.hs / "receipts" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "999Z-1-abcdef.json")).write_text(
        json.dumps({"ticket": "x2", "class": "shared", "wholeMachine": True}), encoding="ascii"), 0.0, "exclusive-admit", 3),
    "exclusive-admit-held": ([], None, lambda w: w.add_holder("x3", 0, whole=True), 0.0, "exclusive-admit", 3),
    "stop-file": ([], None, lambda w: (w.window / "ab-stop").write_text("stop", encoding="ascii"), 0.0, "stop-file", 3),
    # adversary pass r1 (A3): an event probe that cannot be read is never "zero events"
    "event-unreadable": ([], None, lambda w: w.event.write_text("not-a-number", encoding="ascii"), 0.0, "event-2004-unreadable", 5),
    # adversary pass r1 (A5): a receipt named for an enqueue time BEFORE the window (the ticket queued earlier, finished inside the window)
    "exclusive-admit-receipt-queued-before-window": ([], None, lambda w: (w.hs / "receipts" / "20200101T000000000Z-9-abcdef.json").write_text(
        json.dumps({"ticket": "x5", "class": "shared", "wholeMachine": True}), encoding="ascii"), 0.0, "exclusive-admit", 3),
    # adversary pass r1 (A4): a receipt that is half-written on the first scan and valid a second later is not marked seen
    "exclusive-admit-receipt-unreadable-first": ([], None, lambda w: _receipt_unreadable_then_valid(w), 1.0, "exclusive-admit", 9),
}


def _run_abort(root: Path, name: str, me: dict) -> dict:
    extra, setup, flip, delay, _reason, _cap = ABORTS[name]
    w = World(root, name, me)
    result = {"name": name, "w": w}
    try:
        if setup:
            setup(w)
        proc = w.start(0.2, 0.2, extra)
        result["proc"] = proc
        sees_b = _wait(lambda: (w.switch_doc() or {}).get("state") == "B" and (w.switch_doc() or {}).get("count") == 2, 60)
        result["saw_b"] = bool(sees_b)
        result["reader_before"] = _reader_counts(root / name, w.switch) if sees_b else None
        flipped = datetime.now(UTC)
        flip(w)
        result["flipped"] = flipped
        try:
            result["exit"] = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            result["exit"] = "timeout"
        result["after_flip"] = [s for s in w.samples() if datetime.strptime(s["utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC) > flipped]
        result["receipt"] = w.receipt()
        result["switch"] = w.switch_doc()
        result["reader_after"] = _reader_counts(root / name, w.switch)
        result["text"] = w.text()
    finally:
        w.close()
    return result


@pytest.fixture(scope="module")
def abort_results(tmp_path_factory, me):
    root = tmp_path_factory.mktemp("aborts")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ABORTS)) as pool:
        futures = {name: pool.submit(_run_abort, root, name, me) for name in ABORTS}
        return {name: fut.result() for name, fut in futures.items()}


@pytest.mark.parametrize("name", sorted(ABORTS))
def test_each_hard_abort_reverts_the_switch_and_names_its_reason(abort_results, name):
    r = abort_results[name]
    _extra, _setup, _flip, _delay, reason, max_samples = ABORTS[name]
    assert r["saw_b"], f"the driver never reached a B block: {r['text']}"
    assert r["reader_before"] == 2, "the reader must read 2 during the B block, before the abort condition"
    assert r["exit"] == 3, r["text"]
    assert r["receipt"] is not None and r["receipt"]["state"] == "ABORTED", r["text"]
    assert r["receipt"]["reason"].startswith(reason), r["receipt"]
    assert r["receipt"]["switchFinal"] == "REVERTED"
    assert r["switch"]["state"] == "REVERTED" and r["switch"]["count"] == 1
    assert r["reader_after"] == 1
    assert len(r["after_flip"]) <= max_samples, f"{len(r['after_flip'])} polls after the condition (cap {max_samples})"
    assert f"AB_ABORT reason={reason}" in r["text"]


# ---------------------------------------------------------------------------------------------------------------------------
# 3. + 6. normal end (REVERTED and DONE) and block alternation A,B,A,B...  (one run: 6 blocks of 4.2 s)
# ---------------------------------------------------------------------------------------------------------------------------
def test_a_normal_window_alternates_a_b_and_ends_reverted_and_done(tmp_path, me):
    w = World(tmp_path, "normal", me)
    try:
        proc = w.start(0.007, 0.07)
        seen = []

        def poll():
            doc = w.switch_doc()
            if doc and (not seen or seen[-1] != (doc["state"], doc["count"])):
                seen.append((doc["state"], doc["count"]))
            return proc.poll() is not None

        _wait(poll, 90, 0.05)
        assert proc.wait(timeout=30) == 0, w.text()
        blocks = w.blocks()
        assert [b["state"] for b in blocks] == ["A", "B", "A", "B", "A", "B"]
        assert [b["count"] for b in blocks] == [1, 2, 1, 2, 1, 2]
        assert [b["block"] for b in blocks] == list(range(6))
        assert blocks[0]["state"] == "A", "a window always starts with A"
        receipt = w.receipt()
        assert receipt["state"] == "DONE" and receipt["switchFinal"] == "REVERTED" and receipt["blocks"] == 6
        doc = w.switch_doc()
        assert doc["state"] == "REVERTED" and doc["count"] == 1
        assert _reader_counts(tmp_path, w.switch) == 1
        assert ("B", 2) in seen and seen[0] == ("A", 1), seen
        rows = w.samples()
        assert len(rows) >= 20
        for key in ("utc", "block", "state", "holders", "labels", "slots", "temp_c", "cpu_pct", "package_w", "free_commit_gb", "free_phys_gb", "c_free_gib"):
            assert key in rows[0], key
        assert '"state":"DONE"' in (w.window / "ab-receipt.json").read_text(encoding="utf-8")
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# 4. killing the driver (the test's own child handle) turns the reader to 1 at once
# ---------------------------------------------------------------------------------------------------------------------------
def test_killing_the_driver_makes_the_reader_return_1_at_once(tmp_path, me):
    w = World(tmp_path, "kill", me)
    try:
        proc = w.start(0.2, 0.2)
        assert _wait(lambda: (w.switch_doc() or {}).get("state") == "B", 60), w.text()
        assert _reader_counts(tmp_path, w.switch) == 2
        proc.kill()
        proc.wait(timeout=30)
        doc = w.switch_doc()
        assert doc["count"] == 2 and doc["state"] == "B", "a killed driver cannot revert the file, so the reader must not trust it"
        assert _reader_counts(tmp_path, w.switch) == 1
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# 5. every preflight refusal leaves no switch at count 2 and writes nothing else
# ---------------------------------------------------------------------------------------------------------------------------
REFUSALS = {
    "sha-mismatch": (dict(pin_ok=False), "heavy-slot-sha-mismatch"),
    "marker-missing": (dict(marker=False), "fast-suites-reader-marker-missing"),
    "feed-stale": (dict(feed_fresh=False), "feed-stale"),
    "low-commit": (dict(), "low-commit"),
    "low-disk": (dict(), "low-disk"),
}


def _run_refusal(root: Path, name: str, me: dict) -> dict:
    kwargs, _reason = REFUSALS[name]
    w = World(root, name, me, **kwargs)
    try:
        if name == "low-commit":
            w.set_memory(12.0, 60.0)
        if name == "low-disk":
            w.set_disk(39.0)
        w.switch.write_text(json.dumps(switch_doc(me)), encoding="ascii")   # a stale count-2 switch left behind by something else
        before = _reader_counts(root / name, w.switch)
        out = subprocess.run(w.args(0.05, 0.05), capture_output=True, text=True, timeout=120)
        return {"w": w, "before": before, "exit": out.returncode, "text": out.stdout + out.stderr, "after": _reader_counts(root / name, w.switch),
                "switch": w.switch_doc(), "window_exists": w.window.exists()}
    finally:
        w.close()


@pytest.fixture(scope="module")
def refusal_results(tmp_path_factory, me):
    root = tmp_path_factory.mktemp("refusals")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(REFUSALS)) as pool:
        futures = {name: pool.submit(_run_refusal, root, name, me) for name in REFUSALS}
        return {name: fut.result() for name, fut in futures.items()}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_every_preflight_refusal_leaves_no_switch_at_count_2(refusal_results, name):
    r = refusal_results[name]
    assert r["before"] == 2, "the test's own pre-seeded switch must read 2 before the driver runs"
    assert r["exit"] == 2, r["text"]
    assert f"AB_REFUSED reason={REFUSALS[name][1]}" in r["text"], r["text"]
    assert r["after"] == 1 and r["switch"]["count"] == 1 and r["switch"]["state"] == "REVERTED"
    assert not r["window_exists"], "a refusal writes nothing else: no window dir, no samples, no receipt"
    assert "AB_BLOCK" not in r["text"]


def test_a_second_driver_is_refused_and_leaves_the_first_ones_switch_alone(tmp_path, me):
    w = World(tmp_path, "twice", me)
    try:
        first = w.start(0.05, 0.05)
        assert _wait(lambda: (w.switch_doc() or {}).get("driverPid") == first.pid, 60), w.text()
        w2_window = w.root / "window2"
        args = w.args(0.05, 0.05)
        args[args.index("-WindowDir") + 1] = str(w2_window)
        second = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert second.returncode == 2 and "AB_REFUSED reason=driver-running" in second.stdout
        assert not w2_window.exists()
        doc = w.switch_doc()
        assert doc["driverPid"] == first.pid and doc["state"] in ("A", "B"), "the first driver's switch must not be rewritten by the refused one"
        (w.window / "ab-stop").write_text("stop", encoding="ascii")
        assert first.wait(timeout=60) == 3
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# 8. static: nothing in the folder can reach the live slot or a process census
# ---------------------------------------------------------------------------------------------------------------------------
def test_no_file_in_the_folder_touches_the_live_slot_or_a_process_census():
    files = sorted(p for p in AB_DIR.iterdir() if p.is_file())
    assert {p.name for p in files} >= {"HeavySlotAb.psm1", "Invoke-HeavySlotAbDriver.ps1", "Measure-HeavySlotAb.ps1", "README.md"}
    forbidden = {
        "status passed as a heavy-slot verb": re.compile(r"heavy-slot(?:\.cmd|\.ps1)?['\"`]?\s+status\b|['\"]status['\"]", re.I),
        "a Global\\HeavySlot mutex name": re.compile(r"Global\\+HeavySlot", re.I),
        "a mutex wait": re.compile(r"WaitOne|WaitAny|WaitHandle", re.I),
        "a Win32_Process query (filtered or not)": re.compile(r"Win32_Process|wmic", re.I),
        "a heavy-slot launch": re.compile(r"heavy-slot\.cmd'?\s+(?:run|exclusive|status)\b", re.I),
    }
    for path in files:
        text = path.read_text(encoding="utf-8")
        for what, rx in forbidden.items():
            assert not rx.search(text), f"{path.name} contains {what}: {rx.search(text).group(0)!r}"


def test_the_driver_never_removes_the_switch_file():
    text = DRIVER.read_text(encoding="ascii")
    for rx in (r"Remove-Item", r"::Delete\(", r"\.Delete\(", r"Clear-Content", r"\bdel\b", r"\brm\b"):
        assert not re.search(rx, text), rx
    assert "REVERTED" in text and "[IO.File]::Move" in text, "revert is an atomic rewrite"


# ---------------------------------------------------------------------------------------------------------------------------
# 7. the evaluator on synthetic receipts and ledger
# ---------------------------------------------------------------------------------------------------------------------------
T0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=UTC)
BLOCK_MIN = 30
N_BLOCKS = 6


def iso7(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}0Z"


def block_start(k):
    return T0 + timedelta(minutes=BLOCK_MIN * k)


def pct(values, p):
    v = sorted(values)
    return round(v[max(1, math.ceil(p / 100 * len(v))) - 1], 3)


A_BUILD = [100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 800, 1000]
A_PYTEST = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]


def _scale(values, f):
    return [round(v * f, 3) for v in values]


def build_window(root: Path, name: str, *, b_build=None, b_pytest=None, a_hold=60.0, b_hold=62.0, b_temp=85.0, b_freq=4400.0, a_freq=4500.0,
                 a_build=None, a_pytest=None, dead=0, reaped=0, spill=False, receipt_state="DONE", receipt_reason="window-complete",
                 b_commit=60.0, a2_holders=1):
    a_build = A_BUILD if a_build is None else a_build
    a_pytest = A_PYTEST if a_pytest is None else a_pytest
    b_build = _scale(A_BUILD, 0.5) if b_build is None else b_build
    b_pytest = _scale(A_PYTEST, 0.2) if b_pytest is None else b_pytest
    win = root / name / "window"
    hs = root / name / "hs"
    (hs / "receipts").mkdir(parents=True)
    win.mkdir(parents=True)
    ledger = []
    blocks = []
    arms = {"A": [0, 2, 4], "B": [1, 3, 5]}
    for k in range(N_BLOCKS):
        blocks.append({"utc": iso(block_start(k)), "block": k, "state": "AB"[k % 2], "count": 1 + k % 2, "startUtc": iso(block_start(k)),
                       "endUtc": iso(block_start(k + 1)), "windowStartUtc": iso(T0), "windowEndUtc": iso(block_start(N_BLOCKS))})
    counter = [0]

    def ticket(state, blk, kind, wait, hold, slot_count):
        counter[0] += 1
        tid = f"t{counter[0]:03d}"
        enq = block_start(blk) + timedelta(seconds=30 * (counter[0] % 9 + 1))
        adm = enq + timedelta(seconds=wait)
        fin = adm + timedelta(seconds=hold)
        label = f"mlv:CARD{counter[0] % 3}:{kind}"
        rec = {"ticket": tid, "label": label, "class": "shared", "wholeMachine": False, "slotName": "Global\\HeavySlot", "slotCount": slot_count, "slots": [0],
               "enqueuedAt": iso7(enq), "admittedAt": iso7(adm), "finishedAt": iso7(fin), "waitedMs": int(wait * 1000), "exit": {"source": "child", "code": 0}}
        (hs / "receipts" / f"{tid}.json").write_text(json.dumps(rec), encoding="ascii")
        ledger.append({"at": iso7(adm), "event": "admit", "ticket": tid, "label": label, "class": "shared", "waitedMs": int(wait * 1000)})
        ledger.append({"at": iso7(fin), "event": "exit", "ticket": tid, "label": label, "source": "child", "code": 0})

    for state, ws_build, ws_pytest, hold, sc in (("A", a_build, a_pytest, a_hold, 1), ("B", b_build, b_pytest, b_hold, 2)):
        for i, w in enumerate(ws_build):
            ticket(state, arms[state][i % 3], "build", w, hold, 1)
        for i, w in enumerate(ws_pytest):
            ticket(state, arms[state][i % 3], "pytest", w, 40.0, sc)
    if spill:   # enqueued 20 s before the A/B edge, admitted 5 s after it: a wait that crosses a block edge
        counter[0] += 1
        enq = block_start(1) - timedelta(seconds=20)
        adm = block_start(1) + timedelta(seconds=5)
        rec = {"ticket": "spill1", "label": "mlv:SPILL:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "slots": [0], "enqueuedAt": iso7(enq),
               "admittedAt": iso7(adm), "finishedAt": iso7(adm + timedelta(seconds=60)), "waitedMs": 25000, "exit": {"source": "child", "code": 0}}
        (hs / "receipts" / "spill1.json").write_text(json.dumps(rec), encoding="ascii")
        ledger.append({"at": iso7(adm), "event": "admit", "ticket": "spill1", "label": "mlv:SPILL:build", "class": "shared"})
        ledger.append({"at": iso7(adm + timedelta(seconds=60)), "event": "exit", "ticket": "spill1", "label": "mlv:SPILL:build", "source": "child", "code": 0})
    for i in range(dead):    # admitted, never exited, no receipt: a holder that died
        ledger.append({"at": iso7(block_start(1) + timedelta(minutes=2 + i)), "event": "admit", "ticket": f"dead{i}", "label": "mlv:DEAD:pytest", "class": "shared", "waitedMs": 9999000})
    for i in range(reaped):  # a held record the status sweep reaped
        ledger.append({"at": iso7(block_start(2) + timedelta(minutes=2 + i)), "event": "admit", "ticket": f"reaped{i}", "label": "mlv:REAPED:build", "class": "shared", "waitedMs": 8888000})
        ledger.append({"at": iso7(block_start(2) + timedelta(minutes=20 + i)), "event": "reaped", "kind": "held", "ticket": f"reaped{i}", "pid": 1})
    ledger.append({"at": iso7(T0 - timedelta(hours=3)), "event": "admit", "ticket": "before1", "label": "mlv:OLD:build", "class": "shared", "waitedMs": 7777000})
    (hs / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in ledger) + "\n", encoding="ascii")
    (win / "ab-blocks.jsonl").write_text("\n".join(json.dumps(b) for b in blocks) + "\n", encoding="ascii")
    samples = []
    for k in range(N_BLOCKS):
        state = "AB"[k % 2]
        for m in range(BLOCK_MIN):
            hot = state == "B" and b_temp >= 90 and m % 2 == 0
            samples.append({"utc": iso(block_start(k) + timedelta(minutes=m)), "block": k, "state": state, "holders": 2 if state == "B" else (a2_holders if k == 2 else 1),
                            "temp_c": b_temp if state == "B" and (b_temp < 90 or hot) else 70.0 if state == "B" else 72.0, "cpu_pct": 60.0,
                            "freq_mhz": b_freq if state == "B" else a_freq, "free_commit_gb": b_commit if state == "B" else 65.0})
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(s) for s in samples) + "\n", encoding="ascii")
    (win / "ab-receipt.json").write_text(json.dumps({"state": receipt_state, "reason": receipt_reason, "windowStartUtc": iso(T0), "windowEndUtc": iso(block_start(N_BLOCKS)),
                                                     "endedUtc": iso(block_start(N_BLOCKS)), "pollSeconds": 60.0, "blocks": N_BLOCKS}), encoding="ascii")
    (root / name / "event.txt").write_text("0", encoding="ascii")
    return win, hs


def evaluate(root: Path, name: str, win: Path, hs: Path, now=None, extra=()):
    now = now or (block_start(N_BLOCKS) + timedelta(hours=2))
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(root / name / "event.txt"),
               "-Now", iso(now), *extra])
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads((win / "ab-verdict.json").read_text(encoding="utf-8")), out.stdout


def test_evaluator_adopts_when_every_threshold_holds_and_prints_them(tmp_path):
    win, hs = build_window(tmp_path, "adopt", dead=2, reaped=1, spill=True)
    v, out = evaluate(tmp_path, "adopt", win, hs)
    assert v["verdict"] == "ADOPT", v
    assert out.startswith("AB_VERDICT ADOPT")
    assert v["thresholds"]["b_wait_p90_max_ratio_of_a"] == 0.75 and v["thresholds"]["min_holders2_minutes"] == 30
    assert v["thresholds"]["holders2_temp_c_p95_max"] == 94 and v["thresholds"]["holders2_freq_p50_min_ratio_of_holders1"] == 0.9
    assert all(c["pass"] for c in v["checks"]) and {c["name"] for c in v["checks"]} >= {"overall_wait_p90_b_vs_a", "build_wait_p90_b_vs_a", "build_hold_p50_b_vs_a",
                                                                                         "holders2_temp_ge90_frac", "holders2_temp_c_p95", "holders2_freq_p50_under_load_vs_holders1", "no_event_2004", "min_free_commit_gb_in_b"}


def test_evaluator_excludes_dead_holders_and_spill_and_counts_them(tmp_path):
    win, hs = build_window(tmp_path, "excl", dead=2, reaped=1, spill=True)
    v, _ = evaluate(tmp_path, "excl", win, hs)
    assert v["dead_holders"]["count"] == 3 and sorted(v["dead_holders"]["tickets"]) == ["dead0", "dead1", "reaped0"]
    assert v["spill"]["count"] == 1 and v["spill"]["tickets"][0]["ticket"] == "spill1"
    assert v["outside_window_or_block"] == 0
    a, b = v["arms"]["A"], v["arms"]["B"]
    # the dead holders' 9999 s / 8888 s waits and the spill ticket would show up in these figures if they leaked in
    assert a["build"]["n"] == 15 and a["build"]["wait_s_p90"] == pct(A_BUILD, 90) == 800
    assert a["pytest"]["n"] == 12 and a["pytest"]["wait_s_p90"] == pct(A_PYTEST, 90)
    assert b["build"]["n"] == 15 and b["build"]["wait_s_p90"] == pct(_scale(A_BUILD, 0.5), 90)
    assert a["overall"]["n"] == 27 and a["overall"]["wait_s_p90"] == pct(A_BUILD + A_PYTEST, 90)
    assert a["overall"]["wait_s_p99"] == pct(A_BUILD + A_PYTEST, 99) and a["overall"]["wait_s_p50"] == pct(A_BUILD + A_PYTEST, 50)
    assert a["build"]["hold_s_p50"] == 60.0 and b["build"]["hold_s_p50"] == 62.0 and a["pytest"]["hold_s_p50"] == 40.0
    assert v["b_pytest"] == {"tickets": 12, "ran_at_slotcount_2": 12}


def test_evaluator_before_the_dead_holder_grace_calls_them_pending_not_dead(tmp_path):
    win, hs = build_window(tmp_path, "early", dead=2)
    v, _ = evaluate(tmp_path, "early", win, hs, now=block_start(N_BLOCKS) + timedelta(minutes=10))
    assert v["window"]["evaluatedEarly"] is True
    assert v["dead_holders"]["count"] == 0 and v["dead_holders"]["pending_before_grace"] == 2


def test_evaluator_rejects_when_waits_do_not_fall(tmp_path):
    win, hs = build_window(tmp_path, "slow", b_build=_scale(A_BUILD, 0.95), b_pytest=_scale(A_PYTEST, 0.95))
    v, _ = evaluate(tmp_path, "slow", win, hs)
    assert v["verdict"] == "REJECT" and "overall_wait_p90_b_vs_a" in v["reason"], v
    assert not next(c for c in v["checks"] if c["name"] == "overall_wait_p90_b_vs_a")["pass"]


def test_evaluator_rejects_when_the_second_slot_runs_hot(tmp_path):
    win, hs = build_window(tmp_path, "hot", b_temp=96.0)
    v, _ = evaluate(tmp_path, "hot", win, hs)
    assert v["verdict"] == "REJECT", v
    assert "holders2_temp_c_p95" in v["reason"] and "holders2_temp_ge90_frac" in v["reason"]
    assert v["samples_by_live_holders"]["holders_2_plus"]["temp_ge90_frac"] == 0.5


def test_evaluator_rejects_when_frequency_collapses_or_builds_slow_down(tmp_path):
    win, hs = build_window(tmp_path, "freq", b_freq=3600.0, b_hold=80.0)
    v, _ = evaluate(tmp_path, "freq", win, hs)
    assert v["verdict"] == "REJECT"
    assert "holders2_freq_p50_under_load_vs_holders1" in v["reason"] and "build_hold_p50_b_vs_a" in v["reason"]


def test_evaluator_rejects_on_low_free_commit_and_on_event_2004(tmp_path):
    win, hs = build_window(tmp_path, "mem", b_commit=9.0)
    v, _ = evaluate(tmp_path, "mem", win, hs)
    assert v["verdict"] == "REJECT" and "min_free_commit_gb_in_b" in v["reason"]
    win, hs = build_window(tmp_path, "ev")
    (tmp_path / "ev" / "event.txt").write_text("2", encoding="ascii")
    v, _ = evaluate(tmp_path, "ev", win, hs)
    assert v["verdict"] == "REJECT" and "no_event_2004" in v["reason"] and v["event_2004"] == 2


def test_evaluator_is_inconclusive_with_too_few_tickets_builds_or_holders2_time(tmp_path):
    win, hs = build_window(tmp_path, "few", a_build=A_BUILD[:4], b_build=_scale(A_BUILD, 0.5)[:4], a_pytest=A_PYTEST[:3], b_pytest=_scale(A_PYTEST, 0.2)[:3])
    v, _ = evaluate(tmp_path, "few", win, hs)
    assert v["verdict"] == "INCONCLUSIVE"
    assert "live closed tickets per arm" in v["reason"] and "builds per arm" in v["reason"]
    # only 25 one-minute holders=2 samples are kept: under the 30 minutes the verdict needs
    win, hs = build_window(tmp_path, "short")
    rows = [json.loads(line) for line in (win / "ab-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    h2 = [r for r in rows if r["holders"] == 2]
    drop = {id(r) for r in h2[25:]}
    kept = [r for r in rows if id(r) not in drop]
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in kept) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "short", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "holders>=2 time" in v["reason"]


def test_evaluator_is_inconclusive_when_the_a_arm_never_queued_or_there_is_no_receipt(tmp_path):
    win, hs = build_window(tmp_path, "noq", a_build=[0] * 15, a_pytest=[0] * 12, b_build=[0] * 15, b_pytest=[0] * 12)
    v, _ = evaluate(tmp_path, "noq", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "no queueing" in v["reason"]
    win, hs = build_window(tmp_path, "norcpt")
    # an unfinished window: the receipt is missing, not DONE
    receipt = win / "ab-receipt.json"
    receipt.write_text(json.dumps({"state": "RUNNING"}), encoding="ascii")
    v, _ = evaluate(tmp_path, "norcpt", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "receipt state RUNNING" in v["reason"]


def test_evaluator_carries_an_aborted_receipt_through(tmp_path):
    win, hs = build_window(tmp_path, "aborted", receipt_state="ABORTED", receipt_reason="thermal-cap temp>=95C for 61s in 300s")
    v, out = evaluate(tmp_path, "aborted", win, hs)
    assert v["verdict"] == "ABORTED" and v["reason"] == "thermal-cap temp>=95C for 61s in 300s"
    assert out.startswith("AB_VERDICT ABORTED")


def test_evaluator_counts_a_ticket_that_timed_out_waiting_in_its_arms_waits(tmp_path):
    win, hs = build_window(tmp_path, "timeout")
    enq = block_start(2) + timedelta(minutes=5)
    rec = {"ticket": "to1", "label": "mlv:SLOW:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "enqueuedAt": iso7(enq), "waitedMs": 1500000}
    (hs / "receipts" / "to1.json").write_text(json.dumps(rec), encoding="ascii")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": iso7(enq + timedelta(seconds=1500)), "event": "tool-exit", "ticket": "to1", "label": "mlv:SLOW:build", "code": 75, "reason": "timed out waiting for the slot"}) + "\n")
    v, _ = evaluate(tmp_path, "timeout", win, hs)
    assert v["arms"]["A"]["timed_out_waiting"] == 1
    assert v["arms"]["A"]["build"]["n"] == 16 and v["arms"]["A"]["build"]["n_closed"] == 15, "a wait that happened counts as a wait, never as a hold"
    assert v["arms"]["A"]["build"]["wait_s_p99"] == 1500.0


def test_the_evaluator_never_writes_under_the_heavy_slot_dir_and_saves_the_headroom_summary(tmp_path):
    win, hs = build_window(tmp_path, "ro")
    fake = tmp_path / "ro" / "headroom.ps1"
    fake.write_text("param([string]$Mode)\nWrite-Output ('{\"mode\":\"' + $Mode + '\"}')\n", encoding="ascii")
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in hs.rglob("*") if p.is_file())
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-HostHeadroomScript", str(fake),
               "-EventProbeFile", str(tmp_path / "ro" / "event.txt"), "-Now", iso(block_start(N_BLOCKS) + timedelta(hours=2))])
    assert out.returncode == 0, out.stderr
    after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in hs.rglob("*") if p.is_file())
    assert before == after
    assert json.loads((win / "ab-headroom-summary.json").read_text(encoding="utf-8")) == {"mode": "Summary"}
    assert (win / "ab-verdict.json").exists()


def test_the_evaluator_waits_for_window_end_plus_the_grace_when_asked(tmp_path):
    win, hs = build_window(tmp_path, "grace", dead=1)
    receipt_path = win / "ab-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["endedUtc"] = iso(datetime.now(UTC))   # the window "ended" just now: the hub arms the evaluator from this receipt
    receipt_path.write_text(json.dumps(receipt), encoding="ascii")
    started = time.monotonic()
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / "grace" / "event.txt"),
               "-WaitForGrace", "-DeadGraceMinutes", "0.04"])
    assert out.returncode == 0, out.stderr
    assert time.monotonic() - started >= 2.0, "it must sit out the 0.04 min (2.4 s) grace before judging"
    v = json.loads((win / "ab-verdict.json").read_text(encoding="utf-8"))
    assert v["window"]["evaluatedEarly"] is False
    assert v["dead_holders"]["count"] == 1 and v["dead_holders"]["pending_before_grace"] == 0


# ---------------------------------------------------------------------------------------------------------------------------
# adversary pass r1 (b0b10c03): one test per finding, fed the finding's own input
# ---------------------------------------------------------------------------------------------------------------------------
def ticks_to_iso(ticks: int) -> str:
    dt = datetime(1, 1, 1) + timedelta(microseconds=ticks // 10)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{ticks % 10_000_000:07d}Z"


def _add_ticket(hs: Path, tid: str, label: str, enq: datetime, adm: datetime, fin: datetime, exit_at: datetime = None):
    rec = {"ticket": tid, "label": label, "class": "shared", "wholeMachine": False, "slotCount": 1, "slots": [0], "enqueuedAt": iso7(enq), "admittedAt": iso7(adm),
           "finishedAt": iso7(fin), "waitedMs": int((adm - enq).total_seconds() * 1000), "exit": {"source": "child", "code": 0}}
    (hs / "receipts" / f"{tid}.json").write_text(json.dumps(rec), encoding="ascii")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": iso7(adm), "event": "admit", "ticket": tid, "label": label, "class": "shared"}) + "\n")
        fh.write(json.dumps({"at": iso7(exit_at or fin), "event": "exit", "ticket": tid, "label": label, "source": "child", "code": 0}) + "\n")


def test_A1_the_driver_refuses_a_window_dir_that_already_holds_a_window(tmp_path, me):
    w = World(tmp_path, "reused", me)
    try:
        w.window.mkdir()
        old = w.window / "ab-blocks.jsonl"
        old.write_text(json.dumps({"block": 0, "state": "A", "startUtc": iso(T0), "endUtc": iso(block_start(1)), "windowStartUtc": iso(T0), "windowEndUtc": iso(block_start(6))}) + "\n", encoding="ascii")
        before = old.read_bytes()
        w.switch.write_text(json.dumps(switch_doc(me)), encoding="ascii")
        out = subprocess.run(w.args(0.05, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=window-dir-used" in out.stdout, out.stdout + out.stderr
        assert old.read_bytes() == before and not (w.window / "ab-samples.jsonl").exists() and not (w.window / "ab-receipt.json").exists()
        assert _reader_counts(tmp_path, w.switch) == 1
    finally:
        w.close()


def test_A1_the_evaluator_does_not_mix_two_windows_found_in_one_dir(tmp_path):
    win, hs = build_window(tmp_path, "mixed")
    later = T0 + timedelta(hours=10)
    rows = [{"utc": iso(later + timedelta(minutes=30 * k)), "block": k, "state": "AB"[k % 2], "count": 1 + k % 2, "startUtc": iso(later + timedelta(minutes=30 * k)),
             "endUtc": iso(later + timedelta(minutes=30 * (k + 1))), "windowStartUtc": iso(later), "windowEndUtc": iso(later + timedelta(hours=3))} for k in range(6)]
    with open(win / "ab-blocks.jsonl", "a", encoding="ascii") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    receipt = json.loads((win / "ab-receipt.json").read_text(encoding="utf-8"))
    receipt.update({"windowStartUtc": iso(later), "windowEndUtc": iso(later + timedelta(hours=3)), "endedUtc": iso(later + timedelta(hours=3))})
    (win / "ab-receipt.json").write_text(json.dumps(receipt), encoding="ascii")
    v, _ = evaluate(tmp_path, "mixed", win, hs, now=later + timedelta(hours=6))
    assert v["verdict"] == "INCONCLUSIVE" and "2 windows" in v["reason"], v


def test_A1_a_receipt_from_another_window_is_not_accepted(tmp_path):
    win, hs = build_window(tmp_path, "otherreceipt")
    receipt = json.loads((win / "ab-receipt.json").read_text(encoding="utf-8"))
    receipt["windowStartUtc"] = iso(T0 + timedelta(days=1))
    (win / "ab-receipt.json").write_text(json.dumps(receipt), encoding="ascii")
    v, _ = evaluate(tmp_path, "otherreceipt", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "another window" in v["reason"], v


def test_A2_an_exit_after_window_end_plus_grace_is_still_a_dead_holder(tmp_path):
    win, hs = build_window(tmp_path, "late")
    end = block_start(N_BLOCKS)
    _add_ticket(hs, "late1", "mlv:LATE:build", end - timedelta(minutes=5), end - timedelta(minutes=1), end + timedelta(minutes=90, seconds=1))
    v, _ = evaluate(tmp_path, "late", win, hs, now=end + timedelta(hours=2))
    assert v["dead_holders"]["count"] == 1 and v["dead_holders"]["tickets"] == ["late1"], v["dead_holders"]
    assert v["arms"]["B"]["build"]["n_closed"] == 15, "the late exit must not enter the statistics"
    # an exit INSIDE the grace is an ordinary closed ticket
    win, hs = build_window(tmp_path, "intime")
    _add_ticket(hs, "late2", "mlv:LATE:build", end - timedelta(minutes=5), end - timedelta(minutes=1), end + timedelta(minutes=89))
    v, _ = evaluate(tmp_path, "intime", win, hs, now=end + timedelta(hours=2))
    assert v["dead_holders"]["count"] == 0 and v["arms"]["B"]["build"]["n_closed"] == 16


def test_A3_an_unreadable_event_probe_makes_the_evaluator_inconclusive_never_adopt(tmp_path):
    win, hs = build_window(tmp_path, "evbad")
    (tmp_path / "evbad" / "event.txt").write_text("not-a-number", encoding="ascii")
    v, _ = evaluate(tmp_path, "evbad", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "event" in v["reason"].lower() and v["event_2004"] is None, v
    missing = tmp_path / "evbad" / "no-such-event-file.txt"
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(missing), "-Now", iso(block_start(N_BLOCKS) + timedelta(hours=2))])
    assert out.returncode == 0 and json.loads((win / "ab-verdict.json").read_text(encoding="utf-8"))["verdict"] == "INCONCLUSIVE"


def test_A6_waits_frequency_and_temperature_are_logged_per_hold_and_split_by_live_holders(tmp_path):
    win, hs = build_window(tmp_path, "perhold", a2_holders=2)
    v, _ = evaluate(tmp_path, "perhold", win, hs)
    split = v["holds_by_holders"]
    # A block 2 carries holders=2 samples: its 9 tickets (5 builds + 4 pytest) overlapped; the other 18 A tickets ran alone; every B ticket overlapped
    assert split["A"]["holders_2_plus"]["n"] == 9 and split["A"]["holders_1"]["n"] == 18, split["A"]
    assert split["B"]["holders_2_plus"]["n"] == 27 and split["B"]["holders_1"]["n"] == 0
    for arm in ("A", "B"):
        for key in ("holders_1", "holders_2_plus"):
            cohort = split[arm][key]
            if cohort["n"]:
                for stat in ("wait_s_p50", "wait_s_p90", "wait_s_p99", "hold_s_p50", "hold_temp_c_max_p95", "hold_freq_mhz_p50"):
                    assert cohort[stat] is not None, (arm, key, stat)
    assert split["B"]["holders_2_plus"]["hold_freq_mhz_p50"] == 4400.0 and split["A"]["holders_1"]["hold_freq_mhz_p50"] == 4500.0
    rows = [json.loads(line) for line in (win / "ab-holds.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 54 and {r["arm"] for r in rows} == {"A", "B"}
    for key in ("ticket", "label", "class", "arm", "block", "wait_s", "hold_s", "holders_peak", "temp_c_max", "freq_mhz_p50"):
        assert key in rows[0], key
    assert sum(1 for r in rows if r["holders_peak"] >= 2) == 36


def test_A7_a_second_holder_seen_only_at_alternate_polls_does_not_count_as_sustained_overlap(tmp_path, me):
    w = World(tmp_path, "alternate", me)
    try:
        w.feed.temp = 91.0
        w.add_holder("h0", 0)
        second = w.hs / "held" / "h1.json"
        aside = w.root / "h1.aside"
        w.add_holder("h1", 1)
        os.replace(second, aside)
        proc = w.start(0.2, 0.2, ["-WarmWindowSeconds", "2", "-WarmFraction", "0.3"])
        seen = 0
        present = False
        end = time.monotonic() + 60
        while time.monotonic() < end and proc.poll() is None:
            n = len(w.samples())
            if n > seen:
                seen = n
                present = not present
                if present:
                    os.replace(aside, second)
                else:
                    os.replace(second, aside)
            if seen >= 26:
                break
            time.sleep(0.02)
        (w.window / "ab-stop").write_text("stop", encoding="ascii")
        assert proc.wait(timeout=60) == 3
        receipt = w.receipt()
        counts = {s["holders"] for s in w.samples()}
        assert counts == {1, 2}, "the scenario must really alternate"
        assert receipt["reason"].startswith("stop-file"), f"overlap seen at alternate polls was counted as sustained: {receipt['reason']}"
    finally:
        w.close()


def test_A8_a_held_record_whose_pid_start_is_a_fraction_of_a_second_off_is_not_a_holder(tmp_path, me):
    w = World(tmp_path, "pidreuse", me)
    try:
        w.add_holder("stale", 0, cls="exclusive", whole=True)
        rec_path = w.hs / "held" / "stale.json"
        rec = json.loads(rec_path.read_text(encoding="ascii"))
        rec["pidStart"] = ticks_to_iso(me["ticks"] + 5_000_000)   # the reused pid started half a second after the record says
        rec_path.write_text(json.dumps(rec), encoding="ascii")
        proc = w.start(0.2, 0.2)
        assert _wait(lambda: len(w.samples()) >= 6, 60), w.text()
        (w.window / "ab-stop").write_text("stop", encoding="ascii")
        assert proc.wait(timeout=60) == 3
        assert w.receipt()["reason"].startswith("stop-file"), w.receipt()
        assert {s["holders"] for s in w.samples()} == {0}
        # the same record with the EXACT start is a live exclusive holder and aborts
        w2 = World(tmp_path, "pidexact", me)
        try:
            w2.add_holder("live", 0, cls="exclusive", whole=True)
            out = subprocess.run(w2.args(0.2, 0.2), capture_output=True, text=True, timeout=120)
            assert out.returncode == 3 and "AB_ABORT reason=exclusive-admit" in out.stdout, out.stdout + out.stderr
        finally:
            w2.close()
    finally:
        w.close()


def test_A9_a_final_revert_that_fails_is_never_recorded_as_reverted(tmp_path, me):
    w = World(tmp_path, "revertfail", me)
    holder = None
    try:
        proc = w.start(0.012, 0.1)
        assert _wait(lambda: (w.switch_doc() or {}).get("state") == "B", 60), w.text()
        holder = open(w.switch, "rb")   # read-shared, not delete-shared: the atomic replace over it cannot succeed
        code = proc.wait(timeout=90)
        receipt = w.receipt()
        text = w.text()
        holder.close()
        holder = None
        assert code == 4, (code, text)
        assert receipt["state"] == "ABORTED" and receipt["switchFinal"] == "REVERT_FAILED" and "revert-failed" in receipt["reason"], receipt
        assert "AB_REVERT_FAILED" in text
        doc = w.switch_doc()
        assert doc["count"] == 2 and doc["state"] == "B", "the test really did block the rewrite"
        assert _reader_counts(tmp_path, w.switch) == 1, "a driver that has exited can never leave the reader at 2"
    finally:
        if holder:
            holder.close()
        w.close()


def test_A10_a_blocks_file_with_no_usable_row_returns_the_typed_no_input_exit(tmp_path):
    win, hs = build_window(tmp_path, "noinput")
    (win / "ab-blocks.jsonl").write_text("{}\n", encoding="ascii")
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / "noinput" / "event.txt")])
    assert out.returncode == 2 and "AB_EVAL_NO_INPUT" in out.stdout, out.stdout + out.stderr


def test_a_reader_that_keeps_opening_the_switch_never_ends_the_window(tmp_path, me):
    """The driver's atomic rewrite must survive a reader that has the file open without delete sharing (Python's open does not share delete)."""
    w = World(tmp_path, "busyreader", me)
    stop = threading.Event()
    reads = [0]

    def hammer():
        while not stop.is_set():
            try:
                with open(w.switch, "rb") as fh:
                    fh.read()
                reads[0] += 1
            except OSError:
                pass

    thread = threading.Thread(target=hammer, daemon=True)
    try:
        proc = w.start(0.01, 0.07)     # 36 s window, 4.2 s blocks: 8 rewrites while the reader hammers the file
        assert _wait(lambda: w.switch.exists(), 60)
        thread.start()
        code = proc.wait(timeout=120)
        stop.set()
        thread.join(timeout=5)
        assert code == 0, w.text()
        assert w.receipt()["state"] == "DONE" and w.receipt()["switchFinal"] == "REVERTED"
        assert reads[0] > 100, "the reader must really have been busy"
    finally:
        stop.set()
        w.close()
