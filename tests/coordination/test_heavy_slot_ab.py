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
        # adversary r2 A8: a live B driver whose window has not begun (a clock stepped back) must not enable count 2
        "not-started": (switch_doc(me, windowStartUtc=iso(now + timedelta(hours=1)), endUtc=iso(now + timedelta(hours=2))), 1),
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

    def args(self, hours, block_minutes, extra=(), poll=POLL):
        return [
            PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(DRIVER),
            "-WindowDir", str(self.window), "-Hours", str(hours), "-BlockMinutes", str(block_minutes), "-PollSeconds", str(poll),
            "-SwitchPath", str(self.switch), "-HeavySlotDir", str(self.hs), "-CoreTempFeed", str(self.feed_path),
            "-MemoryProbeFile", str(self.memory), "-EventProbeFile", str(self.event), "-DiskProbeFile", str(self.disk),
            "-FastSuitesPath", str(self.fast), "-PinSha", self.pin, "-MutexName", self.mutex, "-NoPerfProbe", *extra,
        ]

    def start(self, hours, block_minutes, extra=(), poll=POLL):
        out = open(self.stdout, "ab")
        err = open(self.stderr, "ab")
        return subprocess.Popen(self.args(hours, block_minutes, extra, poll), stdout=out, stderr=err)

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
    # r6 (CI run 44298772131 shard 8/8): a 1.0 s stale-feed limit fired BEFORE the flip on a loaded runner (15 drivers at once), so the B
    # read lost the race. 6 s keeps the abort tied to the pause; the cap counts polls at 0.3 s across the 6 s plus slack.
    "stale-feed": (["-FeedAbortSeconds", "6.0"], None, lambda w: setattr(w.feed, "paused", True), 6.0, "stale-feed", 30),
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
    # r6 (a): the atomic temp-and-move lives in the ONE shared writer, which the driver's Write-AtomicText calls
    assert "REVERTED" in text and "Write-HeavySlotAbFile -Path $Path -Text $Text" in text, "revert is an atomic rewrite through the shared writer"
    module = MODULE.read_text(encoding="ascii")
    writer = re.search(r"(?s)function Write-HeavySlotAbFile \{(.*?)\n\}", module).group(1)
    assert "[IO.File]::Move($tmp, $Path, $true)" in writer and "FileMode]::CreateNew" in writer
    assert "[IO.File]::Delete($tmp)" in writer and "Delete($Path" not in writer, "the writer only ever removes its own temp name"


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
                 b_commit=60.0, a2_holders=1, b_slot_count=2):
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

    for state, ws_build, ws_pytest, hold, sc in (("A", a_build, a_pytest, a_hold, 1), ("B", b_build, b_pytest, b_hold, b_slot_count)):
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
    rec = {"ticket": "to1", "label": "mlv:SLOW:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "enqueuedAt": iso7(enq), "waitedMs": 1200000}
    (hs / "receipts" / "to1.json").write_text(json.dumps(rec), encoding="ascii")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": iso7(enq + timedelta(seconds=1200)), "event": "tool-exit", "ticket": "to1", "label": "mlv:SLOW:build", "code": 75, "reason": "timed out waiting for the slot"}) + "\n")
    v, _ = evaluate(tmp_path, "timeout", win, hs)
    assert v["arms"]["A"]["timed_out_waiting"] == 1
    assert v["arms"]["A"]["build"]["n"] == 16 and v["arms"]["A"]["build"]["n_closed"] == 15, "a wait that happened counts as a wait, never as a hold"
    assert v["arms"]["A"]["build"]["wait_s_p99"] == 1200.0


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
        proc = w.start(0.012, 0.1, ["-SwitchRetrySeconds", "1"])   # a short retry budget: this test holds the file for good
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


# ---------------------------------------------------------------------------------------------------------------------------
# adversary pass r2 (cdb24411): one test per finding, fed the finding's own input
# ---------------------------------------------------------------------------------------------------------------------------
def _append_sample(win: Path, row: dict):
    with open(win / "ab-samples.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps(row) + "\n")


def test_r2_A1_a_receipt_that_closed_after_the_grace_is_dead_even_without_a_ledger_admit_row(tmp_path):
    win, hs = build_window(tmp_path, "norow")
    end = block_start(N_BLOCKS)
    rec = {"ticket": "late1", "label": "mlv:LATE:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "slots": [0], "enqueuedAt": iso7(end - timedelta(minutes=5)),
           "admittedAt": iso7(end - timedelta(minutes=1)), "finishedAt": iso7(end + timedelta(minutes=90, seconds=1)), "waitedMs": 240000, "exit": {"source": "child", "code": 0}}
    (hs / "receipts" / "late1.json").write_text(json.dumps(rec), encoding="ascii")    # no admit and no exit row for it in the ledger at all
    v, _ = evaluate(tmp_path, "norow", win, hs, now=end + timedelta(hours=2))
    assert v["dead_holders"]["count"] == 1 and v["dead_holders"]["tickets"] == ["late1"], v["dead_holders"]
    assert v["arms"]["B"]["build"]["n_closed"] == 15
    assert not any(json.loads(line)["ticket"] == "late1" for line in (win / "ab-holds.jsonl").read_text(encoding="utf-8").splitlines())


def test_r2_A2_the_blocks_log_is_the_state_really_applied_when_the_host_is_slow(tmp_path, me):
    w = World(tmp_path, "slowhost", me)
    try:
        proc = w.start(0.004, 0.02, ["-ProbeDelaySeconds", "2.0"])   # 14.4 s window, 1.2 s blocks, every poll stalls 2 s
        assert proc.wait(timeout=120) == 0, w.text()
        rows = w.blocks()
        assert len(rows) >= 3, rows
        parse = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        start = parse(rows[0]["windowStartUtc"])
        for r in rows:
            assert abs((parse(r["utc"]) - parse(r["startUtc"])).total_seconds()) < 0.3, f"startUtc must be when the state was applied: {r}"
            idx = int((parse(r["startUtc"]) - start).total_seconds() // 1.2)
            assert r["block"] in (idx, idx - 1) and r["state"] == "AB"[r["block"] % 2], r
        assert [r["block"] for r in rows] == sorted(r["block"] for r in rows)
    finally:
        w.close()


def test_r2_A2_the_evaluator_attributes_by_when_a_block_was_applied_not_by_the_schedule(tmp_path):
    win, hs = build_window(tmp_path, "latehost")
    rows = [json.loads(line) for line in (win / "ab-blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[1]["startUtc"] = iso(block_start(1) + timedelta(minutes=10))     # block 1 (B) was applied 10 minutes late
    (win / "ab-blocks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    enq = block_start(1) + timedelta(minutes=5)
    _add_ticket(hs, "lateB", "mlv:LATE:build", enq, enq + timedelta(seconds=20), enq + timedelta(seconds=80))
    v, _ = evaluate(tmp_path, "latehost", win, hs)
    holds = {json.loads(line)["ticket"]: json.loads(line) for line in (win / "ab-holds.jsonl").read_text(encoding="utf-8").splitlines()}
    assert holds["lateB"]["arm"] == "A", "at +5 min block 1 had not been applied yet: the state in force was A"
    assert holds["lateB"]["block"] == 0
    # every B ticket enqueued in block 1's first 4.5 min now belongs to the A that was really in force (5 builds), plus lateB itself
    assert v["arms"]["A"]["build"]["n_closed"] == 21 and v["arms"]["B"]["build"]["n_closed"] == 10


def test_r2_A3_an_unguarded_event_after_the_last_poll_still_ends_the_window_aborted(tmp_path, me):
    w = World(tmp_path, "lateevent", me)
    try:
        proc = w.start(0.004, 0.2, poll=5)     # polls at ~0, 5, 10 s; the window ends at 14.4 s
        assert _wait(lambda: w.blocks(), 60)
        end = datetime.strptime(w.blocks()[0]["windowEndUtc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        assert _wait(lambda: datetime.now(UTC) >= end - timedelta(seconds=1.6), 60)
        w.append_ledger({"at": iso(datetime.now(UTC)), "event": "unguarded", "ticket": "late-u", "label": "mlv:X:build", "reason": "t"})
        assert proc.wait(timeout=60) == 3, w.text()
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("unguarded"), w.receipt()
        assert w.switch_doc()["state"] == "REVERTED"
    finally:
        w.close()


def test_r2_A4_a_refusal_whose_revert_cannot_be_proven_exits_4_not_2(tmp_path, me):
    w = World(tmp_path, "refuserevert", me)
    holder = None
    try:
        w.switch.write_text(json.dumps(switch_doc(me)), encoding="ascii")    # a live count-2 switch left by something else
        holder = open(w.switch, "rb")                                        # and a reader that will not let go
        out = subprocess.run(w.args(0.05, 0.05, ["-SwitchRetrySeconds", "1"]), capture_output=True, text=True, timeout=120)
        holder.close()
        holder = None
        assert out.returncode == 4, (out.returncode, out.stdout, out.stderr)
        assert "AB_REVERT_FAILED" in out.stdout and "AB_REFUSED" in out.stdout
        assert not w.window.exists()
    finally:
        if holder:
            holder.close()
        w.close()


def _lock_exclusively(path: Path):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)    # GENERIC_READ, share mode 0 (nothing), OPEN_EXISTING
    assert handle not in (None, wintypes.HANDLE(-1).value), "could not lock the file for the test"
    return lambda: k32.CloseHandle(handle)


@pytest.mark.parametrize("which", ["fast-suites", "heavy-slot-cli"])
def test_r2_A5_a_preflight_read_that_fails_is_a_refusal_not_an_abort(tmp_path, me, which):
    w = World(tmp_path, "locked-" + which, me)
    release = None
    try:
        target = w.fast if which == "fast-suites" else w.hs / "bin" / "heavy-slot.ps1"
        release = _lock_exclusively(target)
        out = subprocess.run(w.args(0.05, 0.05), capture_output=True, text=True, timeout=120)
        release()
        release = None
        assert out.returncode == 2 and "AB_REFUSED reason=preflight-failed" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert not w.window.exists() and not w.switch.exists()
    finally:
        if release:
            release()
        w.close()


def test_r2_A6_unparseable_block_rows_are_no_input_and_unparseable_samples_are_skipped(tmp_path):
    for name, row in (("blockx", {"block": "x", "state": "A", "startUtc": iso(T0), "endUtc": iso(block_start(1)), "windowStartUtc": iso(T0), "windowEndUtc": iso(block_start(6))}),
                      ("statec", {"block": 0, "state": "C", "startUtc": iso(T0), "endUtc": iso(block_start(1)), "windowStartUtc": iso(T0), "windowEndUtc": iso(block_start(6))})):
        win, hs = build_window(tmp_path, name)
        (win / "ab-blocks.jsonl").write_text(json.dumps(row) + "\n", encoding="ascii")
        out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / name / "event.txt")])
        assert out.returncode == 2 and "AB_EVAL_NO_INPUT" in out.stdout, (name, out.returncode, out.stdout, out.stderr)
    win, hs = build_window(tmp_path, "badsample")
    _append_sample(win, {"utc": iso(block_start(1) + timedelta(minutes=1, seconds=30)), "block": 1, "state": "B", "holders": "x", "temp_c": "hot", "cpu_pct": 60.0, "freq_mhz": 4400.0, "free_commit_gb": 60.0})
    _append_sample(win, {"utc": "not-a-date", "holders": 2})
    v, _ = evaluate(tmp_path, "badsample", win, hs)
    # r6 (b) supersedes the r2 "typed skip": an unparseable sample is UNKNOWN evidence and fails the measurement closed
    assert v["verdict"] == "INCONCLUSIVE" and "unknown evidence" in v["reason"], v
    assert v["input_problems"]["unknown_evidence"] == 2, v["input_problems"]


def test_r2_A7_the_driver_never_writes_over_what_it_only_reads(tmp_path, me):
    w = World(tmp_path, "overlap", me)
    try:
        cli = w.hs / "bin" / "heavy-slot.ps1"
        before = cli.read_bytes()
        args = w.args(0.05, 0.05)
        args[args.index("-SwitchPath") + 1] = str(cli)
        out = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=path-overlap" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert cli.read_bytes() == before, "heavy-slot.ps1 was overwritten"
        args = w.args(0.05, 0.05)
        args[args.index("-WindowDir") + 1] = str(w.hs / "window")
        out = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=path-overlap" in out.stdout and not (w.hs / "window").exists(), (out.stdout, out.stderr)
    finally:
        w.close()


def test_r2_A7_the_evaluator_refuses_a_window_dir_under_the_heavy_slot_dir(tmp_path):
    win, hs = build_window(tmp_path, "evaloverlap")
    for item in win.iterdir():
        shutil.copy(item, hs / item.name)
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(hs), "-HeavySlotDir", str(hs), "-SkipHeadroom"])
    assert out.returncode == 2 and "AB_EVAL_REFUSED" in out.stdout, (out.returncode, out.stdout, out.stderr)
    assert not (hs / "ab-verdict.json").exists() and not (hs / "ab-holds.jsonl").exists()


def test_r2_A9_the_holders_minutes_never_run_past_the_window_end(tmp_path):
    win, hs = build_window(tmp_path, "lastsample")
    receipt = json.loads((win / "ab-receipt.json").read_text(encoding="utf-8"))
    receipt["pollSeconds"] = 7200.0
    (win / "ab-receipt.json").write_text(json.dumps(receipt), encoding="ascii")
    row = {"utc": iso(block_start(N_BLOCKS) - timedelta(seconds=60)), "block": 5, "state": "B", "holders": 2, "temp_c": 80.0, "cpu_pct": 60.0, "freq_mhz": 4400.0, "free_commit_gb": 60.0}
    (win / "ab-samples.jsonl").write_text(json.dumps(row) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "lastsample", win, hs)
    minutes = v["samples_by_live_holders"]["holders_2_plus"]["minutes"]
    assert 0.9 <= minutes <= 1.01, f"one sample 60 s before the window end stands for 60 s, not {minutes} min"


def test_r2_A10_a_second_driver_with_another_mutex_name_but_the_same_switch_is_refused(tmp_path, me):
    w = World(tmp_path, "sameswitch", me)
    try:
        first = w.start(0.2, 0.2)
        assert _wait(lambda: (w.switch_doc() or {}).get("driverPid") == first.pid, 60), w.text()
        args = w.args(0.2, 0.2)
        args[args.index("-WindowDir") + 1] = str(w.root / "window-b")
        args[args.index("-MutexName") + 1] = "Global\\MLV-HeavySlotAbDriver-other-" + uuid.uuid4().hex[:8]
        second = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert second.returncode == 2 and "AB_REFUSED reason=driver-running" in second.stdout, (second.returncode, second.stdout, second.stderr)
        assert not (w.root / "window-b").exists()
        assert (w.switch_doc() or {}).get("driverPid") == first.pid and (w.switch_doc() or {}).get("state") in ("A", "B")
        (w.window / "ab-stop").write_text("stop", encoding="ascii")
        assert first.wait(timeout=60) == 3
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# round r4 (head bd40d96d): the CI red, adversary pass 2 (ten majors) and the Dot advisory, each fed its own input
# ---------------------------------------------------------------------------------------------------------------------------
def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def test_r4_ci_a_reader_that_holds_the_switch_for_seconds_does_not_end_the_window(tmp_path, me):
    """CI run 44251460414: the atomic rewrite gave up after ~1 s of 'access denied' and the window ended as driver-exception. A reader
    that keeps the file open past that budget (a slow runner) must be waited out, not abort a 24 h window."""
    w = World(tmp_path, "holdreader", me)
    holder = None
    try:
        proc = w.start(0.01, 0.07)     # 36 s window, 4.2 s blocks
        assert _wait(lambda: (w.switch_doc() or {}).get("state") == "A", 60), w.text()
        holder = open(w.switch, "rb")  # not delete-shared: the rewrite at the 4.2 s edge cannot replace the file while this is open
        time.sleep(7.0)                # past the old 1 s budget by several seconds, well inside the new one
        holder.close()
        holder = None
        code = proc.wait(timeout=120)
        assert code == 0, w.text()
        assert w.receipt()["state"] == "DONE" and w.receipt()["switchFinal"] == "REVERTED"
    finally:
        if holder:
            holder.close()
        w.close()


def _junction(link: Path, target: Path):
    out = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_r4_A1_a_switch_reached_through_a_junction_into_the_heavy_slot_dir_is_refused(tmp_path, me):
    w = World(tmp_path, "junction-driver", me)
    link = w.root / "alias"
    try:
        _junction(link, w.hs)
        args = w.args(0.05, 0.05)
        args[args.index("-SwitchPath") + 1] = str(link / "switch.json")
        out = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=path-overlap" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert not (w.hs / "switch.json").exists() and not (w.hs / "switch.json.lock").exists(), "the driver wrote into the HeavySlot dir through the alias"
    finally:
        if link.exists():
            os.rmdir(link)
        w.close()


def test_r4_A1_an_evaluator_window_dir_reached_through_a_junction_into_the_heavy_slot_dir_is_refused(tmp_path):
    win, hs = build_window(tmp_path, "junction-eval")
    link = tmp_path / "junction-eval" / "alias"
    _junction(link, hs)
    try:
        out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(link / "window"), "-HeavySlotDir", str(hs), "-SkipHeadroom"])
        assert out.returncode == 2 and "AB_EVAL_REFUSED" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert not (hs / "window").exists()
    finally:
        os.rmdir(link)


def test_r4_A2_two_drivers_that_pass_the_unused_check_together_cannot_share_a_window_dir(tmp_path, me):
    w = World(tmp_path, "claimrace", me)
    try:
        procs = []
        for tag in "12":
            a = w.args(0.004, 0.05, ["-WindowClaimDelaySeconds", "3"])
            a[a.index("-SwitchPath") + 1] = str(w.root / f"switch-{tag}.json")
            a[a.index("-MutexName") + 1] = "Global\\MLV-HeavySlotAbDriver-test-" + uuid.uuid4().hex[:12]
            procs.append(subprocess.Popen(a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outs = [p.communicate(timeout=120) for p in procs]
        codes = sorted(p.returncode for p in procs)
        text = "\n".join(o + e for o, e in outs)
        assert codes.count(2) == 1 and "AB_REFUSED reason=window-dir-used" in text, (codes, text)
        assert (w.window / "ab-window.lock").exists()
        rows = w.blocks()
        assert len({r["windowStartUtc"] for r in rows}) == 1, "two windows were written into one directory"
    finally:
        w.close()


def test_r4_A3_a_ledger_that_cannot_be_read_ends_the_window_aborted_not_clean(tmp_path, me):
    w = World(tmp_path, "ledgerlock", me)
    release = None
    try:
        release = _lock_exclusively(w.hs / "ledger.jsonl")
        proc = w.start(0.004, 0.05)
        code = proc.wait(timeout=90)
        release()
        release = None
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("ledger-unreadable"), w.receipt()
        assert w.switch_doc()["state"] == "REVERTED"
    finally:
        if release:
            release()
        w.close()


def test_r4_A3_a_held_record_that_cannot_be_parsed_ends_the_window_aborted(tmp_path, me):
    w = World(tmp_path, "heldbad", me)
    try:
        (w.hs / "held" / "broken.json").write_text('{"ticket": "broken", "pid": ', encoding="ascii")
        code = w.start(0.004, 0.05).wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["reason"].startswith("held-unreadable"), w.receipt()
    finally:
        w.close()


def test_r4_A3_a_receipt_that_stays_unreadable_ends_the_window_aborted(tmp_path, me):
    w = World(tmp_path, "receiptbad", me)
    try:
        proc = w.start(0.004, 0.05, ["-ReceiptGiveUpReads", "3"])
        assert _wait(lambda: w.blocks(), 60), w.text()
        (w.hs / "receipts" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "777Z-3-badbad.json")).write_text('{"ticket": "x', encoding="ascii")
        code = proc.wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["reason"].startswith("receipt-unreadable"), w.receipt()
    finally:
        w.close()


def test_r4_A9_a_final_receipt_that_cannot_be_written_never_reads_as_done(tmp_path, me):
    w = World(tmp_path, "rcptfail", me)
    try:
        proc = w.start(0.004, 0.05)
        assert _wait(lambda: w.blocks(), 60), w.text()
        (w.window / "ab-receipt.json").mkdir()      # something else took the receipt's name after preflight
        code = proc.wait(timeout=90)
        text = w.text()
        assert code == 5 and "AB_RECEIPT_FAILED" in text and "AB_DONE" not in text, (code, text)
        assert w.switch_doc()["state"] == "REVERTED" and _reader_counts(tmp_path, w.switch) == 1
    finally:
        w.close()


def test_r4_A10_the_blocks_log_takes_the_start_before_the_switch_is_written(tmp_path, me):
    w = World(tmp_path, "prestart", me)
    try:
        proc = w.start(0.004, 0.05, ["-PostSwitchDelaySeconds", "2"])    # the driver is descheduled for 2 s right after each write
        seen = {}

        def b_written():
            doc = w.switch_doc()
            if doc and doc.get("state") == "B" and "updatedUtc" in doc:
                seen["updated"] = _parse_iso(doc["updatedUtc"])
                return True
            return False

        assert _wait(b_written, 60), w.text()
        assert _wait(lambda: any(r["block"] == 1 for r in w.blocks()), 60), w.text()
        row = next(r for r in w.blocks() if r["block"] == 1)
        assert _parse_iso(row["startUtc"]) <= seen["updated"] + timedelta(milliseconds=100), f"startUtc {row['startUtc']} is later than the write {seen['updated']}"
        assert "switchWrittenUtc" in row and _parse_iso(row["switchWrittenUtc"]) >= _parse_iso(row["startUtc"])
        proc.wait(timeout=90)
    finally:
        w.close()


def test_r4_A8_a_c_drive_a_mebibyte_under_the_floor_is_refused_not_rounded_up(tmp_path, me):
    w = World(tmp_path, "diskround", me)
    try:
        out = subprocess.run(w.args(0.05, 0.05, ["-DiskProbeBytes", "42948624384"]), capture_output=True, text=True, timeout=120)    # 40 GiB minus 1 MiB
        assert out.returncode == 2 and "AB_REFUSED reason=low-disk" in out.stdout, (out.returncode, out.stdout, out.stderr)
    finally:
        w.close()


def test_r4_A8_holders2_time_a_fraction_under_the_floor_is_inconclusive_not_rounded_up(tmp_path):
    win, hs = build_window(tmp_path, "minround")
    receipt = json.loads((win / "ab-receipt.json").read_text(encoding="utf-8"))
    receipt["pollSeconds"] = 59.9
    (win / "ab-receipt.json").write_text(json.dumps(receipt), encoding="ascii")
    rows = []
    for m in range(5):
        rows.append({"utc": iso(block_start(0) + timedelta(minutes=m)), "block": 0, "state": "A", "holders": 1, "temp_c": 72.0, "cpu_pct": 60.0, "freq_mhz": 4500.0, "free_commit_gb": 65.0})
    for m in range(30):
        rows.append({"utc": iso(block_start(1) + timedelta(minutes=m)), "block": 1, "state": "B", "holders": 2, "temp_c": 70.0, "cpu_pct": 60.0, "freq_mhz": 4400.0, "free_commit_gb": 60.0})
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "minround", win, hs)
    # 29 samples stand 60 s each and the last one 59.9 s: 1799.9 s = 29.998 min, under the 30 min floor (it prints as 30.0)
    assert v["verdict"] == "INCONCLUSIVE" and "holders>=2 time" in v["reason"], v


def test_r4_A4_a_reaped_held_row_without_an_admit_row_still_excludes_its_receipt(tmp_path):
    win, hs = build_window(tmp_path, "reapnoadmit")
    enq = block_start(1) + timedelta(minutes=1)
    rec = {"ticket": "r1", "label": "mlv:REAP:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "slots": [0], "enqueuedAt": iso7(enq),
           "admittedAt": iso7(enq + timedelta(minutes=1)), "finishedAt": iso7(enq + timedelta(minutes=2)), "waitedMs": 60000, "exit": {"source": "child", "code": 0}}
    (hs / "receipts" / "r1.json").write_text(json.dumps(rec), encoding="ascii")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": iso7(enq + timedelta(minutes=1, seconds=30)), "event": "reaped", "kind": "held", "ticket": "r1", "pid": 1}) + "\n")
    v, _ = evaluate(tmp_path, "reapnoadmit", win, hs)
    assert "r1" in v["dead_holders"]["tickets"], v["dead_holders"]
    assert v["arms"]["B"]["build"]["n_closed"] == 15, "a reaped holder's receipt must not enter the statistics"


def test_r4_A5_a_never_admitted_wait_that_ends_in_another_block_is_spill_not_a_timed_out_wait(tmp_path):
    win, hs = build_window(tmp_path, "waitcross")
    enq = block_start(1) - timedelta(seconds=10)
    rec = {"ticket": "to1", "label": "mlv:SLOW:build", "class": "shared", "wholeMachine": False, "slotCount": 1, "enqueuedAt": iso7(enq), "waitedMs": 20000}
    (hs / "receipts" / "to1.json").write_text(json.dumps(rec), encoding="ascii")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": iso7(block_start(1) + timedelta(seconds=10)), "event": "tool-exit", "ticket": "to1", "label": "mlv:SLOW:build", "code": 75, "reason": "timed out waiting for the slot"}) + "\n")
    v, _ = evaluate(tmp_path, "waitcross", win, hs)
    assert v["arms"]["A"]["timed_out_waiting"] == 0 and v["arms"]["B"]["timed_out_waiting"] == 0
    assert v["spill"]["count"] == 1 and v["spill"]["tickets"][0]["ticket"] == "to1"


def test_r4_A6_a_b_sample_with_no_free_commit_reading_is_not_dropped_from_the_minimum(tmp_path):
    win, hs = build_window(tmp_path, "nullcommit")
    rows = [json.loads(line) for line in (win / "ab-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    next(r for r in rows if r["state"] == "B")["free_commit_gb"] = None
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "nullcommit", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "free commit" in v["reason"], v


def test_r4_A7_a_receipt_written_during_the_window_that_cannot_be_read_is_inconclusive(tmp_path):
    win, hs = build_window(tmp_path, "rcptlocked", a_build=[100] * 15, a_pytest=[100] * 12, b_build=[50] * 12 + [800] * 3, b_pytest=[20] * 12)
    bad = hs / "receipts" / "t001.json"
    release = _lock_exclusively(bad)
    try:
        v, _ = evaluate(tmp_path, "rcptlocked", win, hs)
    finally:
        release()
    assert v["verdict"] == "INCONCLUSIVE" and "receipt" in v["reason"].lower() and "cannot be read" in v["reason"], v
    assert v["input_problems"]["receipts_unreadable"] == 1


def test_r4_A7_an_old_unreadable_receipt_and_unparseable_ledger_lines_are_counted_not_fatal(tmp_path):
    win, hs = build_window(tmp_path, "oldbad")
    old = hs / "receipts" / "ancient.json"
    old.write_text("{not json", encoding="ascii")
    ancient = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(old, (ancient, ancient))
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write("this is not json\n")
    v, _ = evaluate(tmp_path, "oldbad", win, hs)
    # r6 (b): the old receipt is still placed out of the window by its write time, but a ledger line that does not parse cannot be placed
    # at all: it is unknown evidence, so the verdict is INCONCLUSIVE (it used to be counted and ignored)
    assert v["verdict"] == "INCONCLUSIVE" and "unknown evidence" in v["reason"] and "ledger.jsonl=1" in v["reason"], v
    assert v["input_problems"]["receipts_unreadable"] == 0 and v["input_problems"]["ledger_lines_unparseable"] == 1, v["input_problems"]


def test_r4_A11_a_holders_value_that_overflows_is_a_typed_skip_not_an_eval_error(tmp_path):
    win, hs = build_window(tmp_path, "overflow")
    _append_sample(win, {"utc": iso(block_start(1) + timedelta(minutes=1, seconds=30)), "block": 1, "state": "B", "holders": 1e20, "temp_c": 80.0, "cpu_pct": 60.0, "freq_mhz": 4400.0, "free_commit_gb": 60.0})
    _append_sample(win, {"utc": iso(block_start(1) + timedelta(minutes=1, seconds=40)), "block": 1, "state": "B", "holders": 2.5, "temp_c": 80.0, "cpu_pct": 60.0, "freq_mhz": 4400.0, "free_commit_gb": 60.0})
    v, out = evaluate(tmp_path, "overflow", win, hs)
    # r6 (b): still no eval error, but an out-of-range or fractional holders value is unknown evidence, never a skip
    assert "AB_EVAL_ERROR" not in out and v["verdict"] == "INCONCLUSIVE" and "unknown evidence" in v["reason"], (out, v)
    assert v["input_problems"]["samples_skipped"] == 2 and v["input_problems"]["unknown_evidence"] == 2, v["input_problems"]


def test_r4_I1_adoption_needs_enough_b_pytest_tickets_that_really_ran_at_slotcount_2(tmp_path):
    win, hs = build_window(tmp_path, "noslot2", b_slot_count=1)
    v, _ = evaluate(tmp_path, "noslot2", win, hs)
    assert v["b_pytest"] == {"tickets": 12, "ran_at_slotcount_2": 0}
    assert v["verdict"] == "INCONCLUSIVE" and "slotCount 2" in v["reason"], v
    assert v["thresholds"]["min_b_pytest_at_slotcount_2"] == 10


def _write_feed_rows(path: Path, rows):
    path.write_text(FEED_HEADER, encoding="ascii")
    with open(path, "a", encoding="ascii") as fh:
        for when in rows:
            fh.write(when.strftime("%d.%m.%Y,%H:%M:%S.") + f"{when.microsecond // 1000:03d}" + ",20.0,60.0,40.0,No,No\n")


def test_r4_B1_a_feed_whose_only_rows_are_in_the_future_is_refused_at_preflight(tmp_path, me):
    w = World(tmp_path, "futurefeed", me)
    try:
        w.feed.close()
        _write_feed_rows(w.feed_path, [datetime.now() + timedelta(hours=1)])
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=feed-future" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert not w.window.exists()
    finally:
        w.close()


def test_r4_B1_a_frozen_future_row_cannot_hide_a_stalled_feed(tmp_path, me):
    w = World(tmp_path, "frozenfuture", me)
    try:
        proc = w.start(0.004, 0.2, ["-FeedAbortSeconds", "1.5"])
        assert _wait(lambda: len(w.samples()) >= 3, 60), w.text()
        w.feed.paused = True
        time.sleep(0.6)
        with open(w.feed_path, "a", encoding="ascii") as fh:
            fut = datetime.now() + timedelta(hours=1)
            fh.write(fut.strftime("%d.%m.%Y,%H:%M:%S.") + f"{fut.microsecond // 1000:03d}" + ",20.0,60.0,40.0,No,No\n")
        code = proc.wait(timeout=90)
        assert code == 3 and w.receipt()["reason"].startswith("stale-feed"), (code, w.text())
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# round r5 (head 5c232129): adversary pass 4 (A1-A7 introduced majors), each fed its own input. Every test fails on 5c232129.
# ---------------------------------------------------------------------------------------------------------------------------
def test_r4b_A1_a_blocks_start_is_the_attempt_that_landed_not_the_start_of_a_retry_loop(tmp_path, me):
    w = World(tmp_path, "retrystart", me)
    holder = None
    try:
        proc = w.start(0.01, 0.07)     # 36 s window, 4.2 s blocks
        assert _wait(lambda: (w.switch_doc() or {}).get("state") == "A", 60), w.text()
        holder = open(w.switch, "rb")  # block 1's rewrite (due at 4.2 s) cannot replace the file while this is open
        time.sleep(7.0)
        released = datetime.now(UTC)
        holder.close()
        holder = None
        assert proc.wait(timeout=120) == 0, w.text()
        row = next(r for r in w.blocks() if r["block"] == 1)
        assert _parse_iso(row["startUtc"]) >= released - timedelta(milliseconds=150), f"startUtc {row['startUtc']} predates the release {released}: tickets run under A would be attributed to B"
    finally:
        if holder:
            holder.close()
        w.close()


@pytest.mark.parametrize("what", ["ledger", "receipts"])
def test_r4b_A2_a_source_that_existed_at_the_baseline_and_is_gone_ends_the_window_aborted(tmp_path, me, what):
    w = World(tmp_path, "gone-" + what, me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        if what == "ledger":
            os.rename(w.hs / "ledger.jsonl", w.hs / "ledger.gone")
        else:
            os.rename(w.hs / "receipts", w.hs / "receipts.gone")
        code = proc.wait(timeout=90)
        want = "ledger-unreadable" if what == "ledger" else "receipt-unreadable"
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith(want), w.receipt()
    finally:
        w.close()


@pytest.mark.parametrize("which,needle", [("ledger", "ledger.jsonl is missing"), ("receipts", "receipts directory is missing")])
def test_r4b_A2_the_evaluator_is_inconclusive_when_the_ledger_or_the_receipts_dir_is_missing(tmp_path, which, needle):
    win, hs = build_window(tmp_path, "missing-" + which)
    if which == "ledger":
        (hs / "ledger.jsonl").unlink()
    else:
        shutil.rmtree(hs / "receipts")
    v, _ = evaluate(tmp_path, "missing-" + which, win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and needle in v["reason"], v


@pytest.mark.parametrize("tail,want", [
    ('{"at":"%s","event":"unguarded","ticket":"u9","label":"mlv:X:build","reason":"t"}', "unguarded"),
    ('{"at": "2026-11', "ledger-unreadable"),
])
def test_r4b_A3_an_unterminated_last_ledger_record_is_read_in_the_final_sweep_never_held_back_for_ever(tmp_path, me, tail, want):
    w = World(tmp_path, "tail-" + want, me)
    try:
        proc = w.start(0.004, 0.2, poll=5)     # polls at ~0, 5, 10 s; the window ends at 14.4 s
        assert _wait(lambda: w.blocks(), 60), w.text()
        end = _parse_iso(w.blocks()[0]["windowEndUtc"])
        assert _wait(lambda: datetime.now(UTC) >= end - timedelta(seconds=1.6), 60)
        text = (tail % iso(datetime.now(UTC))) if "%s" in tail else tail
        with open(w.hs / "ledger.jsonl", "ab") as fh:
            fh.write(text.encode("ascii"))      # no trailing newline
        assert proc.wait(timeout=60) == 3, w.text()
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith(want), w.receipt()
        assert w.switch_doc()["state"] == "REVERTED"
    finally:
        w.close()


def test_r4b_A4_the_switch_temp_file_name_may_not_be_a_read_only_input(tmp_path, me):
    w = World(tmp_path, "tmpalias", me)
    try:
        w.feed.close()
        tmp_feed = Path(str(w.switch) + ".tmp")
        _write_feed_rows(tmp_feed, [datetime.now()])
        before = tmp_feed.read_bytes()
        w.switch.write_text(json.dumps(switch_doc(me)), encoding="ascii")    # an existing switch: the first write is the revert, through <switch>.tmp
        args = w.args(0.004, 0.05)
        args[args.index("-CoreTempFeed") + 1] = str(tmp_feed)
        out = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=path-overlap" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert tmp_feed.read_bytes() == before, "the feed was overwritten through the switch's temp name"
    finally:
        w.close()


def test_r4b_A4_an_evaluator_output_that_is_a_symlink_onto_an_input_is_refused(tmp_path):
    win, hs = build_window(tmp_path, "outalias")
    try:
        os.symlink(hs / "ledger.jsonl", win / "ab-holds.jsonl")
    except OSError:
        pytest.skip("no symlink rights on this host")
    before = (hs / "ledger.jsonl").read_bytes()
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / "outalias" / "event.txt")])
    assert out.returncode == 2 and "AB_EVAL_REFUSED output-alias" in out.stdout, (out.returncode, out.stdout, out.stderr)
    assert (hs / "ledger.jsonl").read_bytes() == before, "the evaluator truncated the ledger through the alias"


@pytest.mark.parametrize("kind", ["receipt-empty-class", "receipt-missing-class", "held-empty-class"])
def test_r4b_A5_a_receipt_or_held_record_with_no_class_is_not_shared(tmp_path, me, kind):
    w = World(tmp_path, kind, me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        if kind.startswith("receipt"):
            rec = {"ticket": "x9", "wholeMachine": False}
            if kind == "receipt-empty-class":
                rec["class"] = ""
            (w.hs / "receipts" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "888Z-1-abcdef.json")).write_text(json.dumps(rec), encoding="ascii")
        else:
            w.add_holder("h9", 0, cls="")
        code = proc.wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("exclusive-admit"), w.receipt()
    finally:
        w.close()


def test_r4b_A6_a_nan_commit_reading_is_unknown_not_above_the_floor(tmp_path, me):
    w = World(tmp_path, "nancommit", me)
    try:
        w.memory.write_text(json.dumps({"freeCommitGB": "NaN", "freePhysGB": "NaN"}), encoding="ascii")
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=low-commit" in out.stdout, (out.returncode, out.stdout, out.stderr)
    finally:
        w.close()


def test_r4b_A6_a_nan_disk_reading_is_unknown_not_above_the_floor(tmp_path, me):
    w = World(tmp_path, "nandisk", me)
    try:
        w.disk.write_text(json.dumps({"freeGiB": "NaN"}), encoding="ascii")
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=low-disk" in out.stdout, (out.returncode, out.stdout, out.stderr)
    finally:
        w.close()


def test_r4b_A6_a_feed_whose_only_temperature_is_nan_is_unreadable_never_cool(tmp_path, me):
    w = World(tmp_path, "nantemp", me)
    try:
        w.feed.close()
        now = datetime.now()
        w.feed_path.write_text(FEED_HEADER + now.strftime("%d.%m.%Y,%H:%M:%S.") + f"{now.microsecond // 1000:03d}" + ",20.0,NaN,40.0,No,No\n", encoding="ascii")
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=feed-unreadable" in out.stdout, (out.returncode, out.stdout, out.stderr)
    finally:
        w.close()


def test_r4b_A6_an_infinite_free_commit_in_b_samples_is_unknown_not_a_pass(tmp_path):
    win, hs = build_window(tmp_path, "infcommit")
    rows = [json.loads(line) for line in (win / "ab-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["state"] == "B":
            r["free_commit_gb"] = "1e999"
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "infcommit", win, hs)
    # r6 (b): a string where a number belongs is a wrong-typed field, so the row is unknown evidence (named by its field)
    assert v["verdict"] == "INCONCLUSIVE" and ("free commit" in v["reason"] or "free_commit_gb" in v["reason"]), v


def test_r4b_A7_a_feed_clock_that_runs_backwards_ends_the_window_closed(tmp_path, me):
    w = World(tmp_path, "clockback", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: len(w.samples()) >= 3, 60), w.text()
        w.feed.paused = True
        time.sleep(0.5)
        with open(w.feed_path, "a", encoding="ascii") as fh:     # a row 25 s ahead (inside the 30 s skew), then correctly dated rows resume below it
            fut = datetime.now() + timedelta(seconds=25)
            fh.write(fut.strftime("%d.%m.%Y,%H:%M:%S.") + f"{fut.microsecond // 1000:03d}" + ",20.0,60.0,40.0,No,No\n")
        time.sleep(1.0)      # at least two polls read the future row first
        w.feed.paused = False
        code = proc.wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("feed-time-regressed"), w.receipt()
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------------------------------------
# round r5 (head 4cb29776): adversary pass 5 (nine introduced majors), each fed its own input. Every test fails on 4cb29776 except the
# one marked GUARD, which pins a fix the A4 change needed (it passes there only because that code skipped the damaged line).
# ---------------------------------------------------------------------------------------------------------------------------
def test_r5_A1_a_cool_future_row_followed_by_a_hot_older_row_is_a_feed_regression(tmp_path, me):
    w = World(tmp_path, "interleave", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: len(w.samples()) >= 3, 60), w.text()
        w.feed.paused = True
        time.sleep(0.5)
        with open(w.feed_path, "a", encoding="ascii") as fh:
            fut = datetime.now() + timedelta(seconds=25)
            hot = datetime.now()
            fh.write(fut.strftime("%d.%m.%Y,%H:%M:%S.") + f"{fut.microsecond // 1000:03d}" + ",20.0,60.0,40.0,No,No\n")     # valid and cool, t+25
            fh.write(hot.strftime("%d.%m.%Y,%H:%M:%S.") + f"{hot.microsecond // 1000:03d}" + ",20.0,96.0,40.0,No,No\n")     # valid and hot, t
        time.sleep(1.0)
        w.feed.paused = False
        code = proc.wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("feed-time-regressed"), w.receipt()
    finally:
        w.close()


def test_r5_A2_a_planted_receipt_temp_name_is_a_leftover_never_a_write_target(tmp_path, me):
    w = World(tmp_path, "tmplink", me)
    try:
        w.window.mkdir()
        try:
            os.symlink(w.feed_path, w.window / "ab-receipt.json.tmp")
        except OSError:
            pytest.skip("no symlink rights on this host")
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "AB_REFUSED reason=window-dir-used" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert FEED_HEADER in w.feed_path.read_text(encoding="ascii") and "heavy-slot-ab-receipt" not in w.feed_path.read_text(encoding="ascii")
    finally:
        w.close()


@pytest.mark.parametrize("what,reason", [("ledger", "ledger-missing"), ("receipts", "receipts-dir-missing")])
def test_r5_A3_a_heavy_slot_with_no_ledger_or_no_receipts_dir_is_refused_not_read_as_no_events(tmp_path, me, what, reason):
    w = World(tmp_path, "absent-" + what, me)
    try:
        if what == "ledger":
            (w.hs / "ledger.jsonl").unlink()
        else:
            shutil.rmtree(w.hs / "receipts")
        out = subprocess.run(w.args(0.004, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and f"AB_REFUSED reason={reason}" in out.stdout, (out.returncode, out.stdout, out.stderr)
        assert not w.window.exists() and not w.switch.exists()
    finally:
        w.close()


def test_r5_A4_a_complete_ledger_line_that_does_not_parse_ends_the_window_aborted(tmp_path, me):
    w = World(tmp_path, "garbageline", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        with open(w.hs / "ledger.jsonl", "ab") as fh:
            fh.write(b'{"at": "2026-11\n')
        code = proc.wait(timeout=90)
        assert code == 3, (code, w.text())
        assert w.receipt()["state"] == "ABORTED" and w.receipt()["reason"].startswith("ledger-unreadable"), w.receipt()
    finally:
        w.close()


def test_r5_A4_GUARD_a_record_still_being_written_at_the_baseline_is_not_a_garbage_line(tmp_path, me):
    w = World(tmp_path, "midwrite", me)
    try:
        (w.hs / "ledger.jsonl").write_bytes(b'{"at":"2020-01-01T00:00:00.000Z","event":"admit","ticket":"old"')      # no newline yet
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        with open(w.hs / "ledger.jsonl", "ab") as fh:
            fh.write(b',"label":"mlv:X:build","class":"shared"}\n')                                                     # the writer finishes it
        assert proc.wait(timeout=90) == 0, w.text()
        assert w.receipt()["state"] == "DONE", w.receipt()
    finally:
        w.close()


def test_r5_A5_a_done_receipt_with_no_window_binding_is_inconclusive(tmp_path):
    win, hs = build_window(tmp_path, "unbound")
    (win / "ab-receipt.json").write_text(json.dumps({"state": "DONE"}), encoding="ascii")
    v, _ = evaluate(tmp_path, "unbound", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "windowStartUtc" in v["reason"] and "endedUtc" in v["reason"], v


def test_r5_A6_a_receipt_with_no_ticket_identity_written_in_the_window_is_inconclusive(tmp_path):
    win, hs = build_window(tmp_path, "emptyreceipt")
    (hs / "receipts" / "unknown.json").write_text("{}", encoding="ascii")
    v, _ = evaluate(tmp_path, "emptyreceipt", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "cannot be read" in v["reason"], v
    assert v["input_problems"]["receipts_unreadable"] == 1


def test_r5_A7_a_sample_with_no_block_state_is_not_dropped_from_the_b_minimum(tmp_path):
    win, hs = build_window(tmp_path, "nostate")
    rows = [json.loads(line) for line in (win / "ab-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    row = next(r for r in rows if r["utc"] == iso(block_start(1)))
    row["state"] = ""
    row["free_commit_gb"] = 1
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "nostate", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "no usable block state" in v["reason"], v


def test_r5_A8_a_retry_that_outlasts_block_edges_lands_the_block_due_now_not_an_obsolete_one(tmp_path, me):
    w = World(tmp_path, "staleblock", me)
    holder = None
    try:
        proc = w.start(0.001, 0.01)      # 3.6 s window, 0.6 s blocks
        assert _wait(lambda: (w.switch_doc() or {}).get("state") == "A", 60), w.text()
        holder = open(w.switch, "rb")    # not delete-shared: every rewrite fails until this is closed, across several block edges
        time.sleep(2.0)
        holder.close()
        holder = None
        assert proc.wait(timeout=60) == 0, w.text()
        rows = w.blocks()
        assert len(rows) >= 2, rows
        ws = _parse_iso(rows[0]["windowStartUtc"])
        idx = lambda t: int((t - ws).total_seconds() // 0.6)
        for r in rows:
            start = _parse_iso(r["startUtc"])
            assert idx(start - timedelta(milliseconds=20)) <= r["block"] <= idx(start), f"block {r['block']} was applied when block {idx(start)} was due: {r}"
            assert r["state"] == "AB"[r["block"] % 2] and r["count"] == 1 + r["block"] % 2, r
    finally:
        if holder:
            holder.close()
        w.close()


def test_r5_A9_an_output_that_is_a_hard_link_onto_an_input_never_truncates_it(tmp_path):
    win, hs = build_window(tmp_path, "hardlink")
    os.link(hs / "ledger.jsonl", win / "ab-holds.jsonl")
    before = (hs / "ledger.jsonl").read_bytes()
    _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / "hardlink" / "event.txt"),
         "-Now", iso(block_start(N_BLOCKS) + timedelta(hours=2))])
    assert (hs / "ledger.jsonl").read_bytes() == before, "the evaluator wrote through a hard link into the HeavySlot ledger"


# ---------------------------------------------------------------------------------------------------------------------------
# round r6 (head b3df3428): adversary pass 6 (A1-A7) fixed BY CLASS. Each class has ONE helper in HeavySlotAb.psm1 that every site
# calls: (a) write safety, (b) row parse (unknown evidence fails closed), (c) ordering (watermark, retry start). One test per class at
# each pass-6 site and one per sibling where a seam allows. Every test below fails on b3df3428's tool files except the 8.3 test and the
# static sweep pin, which are containment / sweep guards.
# ---------------------------------------------------------------------------------------------------------------------------
def _feed_line(when: datetime, temp=60.0) -> str:
    return when.strftime("%d.%m.%Y,%H:%M:%S.") + f"{when.microsecond // 1000:03d}" + f",20.0,{temp},40.0,No,No\n"


def _static_feed(w: World):
    w.feed.close()
    w.feed_path.write_text(FEED_HEADER + _feed_line(datetime.now()), encoding="ascii")
    return w.feed_path.read_bytes()


def test_r6_a_A1_a_planted_switch_tmp_hard_link_onto_the_feed_is_never_truncated(tmp_path, me):
    w = World(tmp_path, "r6tmplink", me)
    try:
        before = _static_feed(w)
        w.switch.write_text("{}", encoding="ascii")                  # an existing switch: the preflight revert is the first write
        os.link(w.feed_path, Path(str(w.switch) + ".tmp"))
        subprocess.run(w.args(0.002, 0.05), capture_output=True, text=True, timeout=120)
        assert w.feed_path.read_bytes() == before, "the feed was truncated through the hard-linked <switch>.tmp"
    finally:
        w.close()


def test_r6_a_A1_sibling_a_receipt_tmp_hard_link_planted_during_the_window_is_never_written_through(tmp_path, me):
    w = World(tmp_path, "r6rcpttmp", me)
    try:
        proc = w.start(0.003, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        os.link(w.feed_path, w.window / "ab-receipt.json.tmp")     # after the preflight check of leftover names
        proc.wait(timeout=90)
        text = w.feed_path.read_text(encoding="ascii", errors="replace")
        assert text.startswith(FEED_HEADER) and "heavy-slot-ab-receipt" not in text, "the receipt write went through the planted temp name"
    finally:
        w.close()


def test_r6_a_A1_sibling_a_samples_file_swapped_for_a_hard_link_is_refused_not_appended_through(tmp_path, me):
    w = World(tmp_path, "r6samplelink", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: len(w.samples()) >= 2, 60), w.text()
        samples = w.window / "ab-samples.jsonl"

        def swap():
            try:
                samples.unlink()
                os.link(w.feed_path, samples)
                return True
            except OSError:
                return False
        assert _wait(swap, 20, 0.01), "could not swap the samples file"
        code = proc.wait(timeout=90)
        assert '"free_commit_gb"' not in w.feed_path.read_text(encoding="ascii", errors="replace"), "a sample was appended into the feed"
        assert code == 3 and "write-alias" in w.receipt()["reason"], (code, w.receipt(), w.text())
    finally:
        w.close()


def test_r6_a_A1_sibling_a_switch_lock_that_is_a_hard_link_onto_the_feed_is_refused(tmp_path, me):
    w = World(tmp_path, "r6locklink", me)
    try:
        _static_feed(w)
        os.link(w.feed_path, Path(str(w.switch) + ".lock"))
        out = subprocess.run(w.args(0.002, 0.05), capture_output=True, text=True, timeout=120)
        assert out.returncode == 2 and "write-alias" in out.stdout, (out.returncode, out.stdout, out.stderr)
    finally:
        w.close()


def test_r6_a_A1_sibling_an_evaluator_output_that_is_a_hard_link_is_refused_up_front(tmp_path):
    win, hs = build_window(tmp_path, "r6verdictlink")
    os.link(hs / "ledger.jsonl", win / "ab-verdict.json")
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", str(win), "-HeavySlotDir", str(hs), "-SkipHeadroom", "-EventProbeFile", str(tmp_path / "r6verdictlink" / "event.txt"),
               "-Now", iso(block_start(N_BLOCKS) + timedelta(hours=2))])
    assert out.returncode == 2 and "AB_EVAL_REFUSED output-alias" in out.stdout, (out.returncode, out.stdout, out.stderr)


def test_r6_a_sweep_every_write_in_the_driver_and_evaluator_goes_through_the_shared_writer():
    for path in (DRIVER, EVALUATOR):
        text = path.read_text(encoding="ascii")
        for raw in ("WriteAllText", "AppendAllText", "FileMode]::Create", "FileMode]::OpenOrCreate", "FileMode]::Append", "Set-Content", "Out-File", "Add-Content"):
            assert raw not in text, f"{path.name} writes with {raw} instead of Write-HeavySlotAbFile / Open-HeavySlotAbLockFile"


def test_r6_b_A3_a_b_sample_with_null_holders_and_low_commit_is_unknown_not_skipped(tmp_path):
    win, hs = build_window(tmp_path, "r6nullholders")
    rows = [json.loads(line) for line in (win / "ab-samples.jsonl").read_text(encoding="utf-8").splitlines()]
    row = next(r for r in rows if r["utc"] == iso(T0 + timedelta(minutes=30)))
    row["holders"] = None
    row["free_commit_gb"] = 1
    (win / "ab-samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    v, _ = evaluate(tmp_path, "r6nullholders", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "unknown evidence" in v["reason"] and "holders" in v["reason"], v


def test_r6_b_A4_a_receipt_with_an_unparseable_finished_at_is_unknown_not_dropped(tmp_path):
    win, hs = build_window(tmp_path, "r6badfinish")
    path = hs / "receipts" / "t054.json"
    rec = json.loads(path.read_text(encoding="ascii"))
    rec["finishedAt"] = "bad"
    path.write_text(json.dumps(rec), encoding="ascii")
    v, _ = evaluate(tmp_path, "r6badfinish", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "cannot be read" in v["reason"] and "t054.json" in v["reason"], v
    assert v["input_problems"]["receipts_unreadable"] == 1


def test_r6_b_A5_a_complete_ledger_record_with_a_null_time_ends_the_window(tmp_path, me):
    w = World(tmp_path, "r6nullat", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        w.append_ledger({"at": None, "event": "unguarded", "ticket": "u1"})
        code = proc.wait(timeout=90)
        assert code == 3 and w.receipt()["reason"].startswith("ledger-unreadable"), (code, w.receipt(), w.text())
    finally:
        w.close()


def test_r6_b_A5_sibling_an_undated_reaped_row_is_unknown_evidence_in_the_evaluator(tmp_path):
    win, hs = build_window(tmp_path, "r6reapednull")
    with open(hs / "ledger.jsonl", "a", encoding="ascii") as fh:
        fh.write(json.dumps({"at": "bad", "event": "reaped", "kind": "held", "ticket": "t030", "pid": 1}) + "\n")
    v, _ = evaluate(tmp_path, "r6reapednull", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "unknown evidence" in v["reason"], v


def test_r6_b_A7_nine_copies_of_one_receipt_are_one_identity_never_nine_tickets(tmp_path):
    win, hs = build_window(tmp_path, "r6copies", b_pytest=[2])
    sole = hs / "receipts" / "t043.json"
    assert json.loads(sole.read_text(encoding="ascii"))["label"].endswith(":pytest")
    for i in range(9):
        shutil.copyfile(sole, hs / "receipts" / f"copy{i}.json")
    v, _ = evaluate(tmp_path, "r6copies", win, hs)
    assert v["verdict"] == "INCONCLUSIVE" and "duplicate ticket t043" in v["reason"], v
    assert v["b_pytest"]["tickets"] == 0, v["b_pytest"]


def test_r6_b_A7_sibling_two_held_records_for_one_ticket_are_never_two_holders(tmp_path, me):
    w = World(tmp_path, "r6heldcopy", me)
    try:
        w.add_holder("d1", 0)
        shutil.copyfile(w.hs / "held" / "d1.json", w.hs / "held" / "d1-copy.json")
        proc = w.start(0.01, 0.2)
        code = proc.wait(timeout=90)
        assert all(s["holders"] < 2 for s in w.samples()), [s["holders"] for s in w.samples()]
        assert code == 3 and w.receipt()["reason"].startswith("held-unreadable"), (code, w.receipt(), w.text())
    finally:
        w.close()


def test_r6_b_sibling_a_receipt_whose_whole_machine_is_not_a_bool_is_unknown_not_shared(tmp_path, me):
    w = World(tmp_path, "r6wholestr", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: w.blocks(), 60), w.text()
        (w.hs / "receipts" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "777Z-1-abcdef.json")).write_text(
            json.dumps({"ticket": "x7", "class": "shared", "wholeMachine": "yes"}), encoding="ascii")
        code = proc.wait(timeout=90)
        assert code == 3 and w.receipt()["reason"].startswith("receipt-unreadable"), (code, w.receipt(), w.text())
    finally:
        w.close()


def test_r6_b_sibling_a_complete_feed_row_with_a_bad_date_is_unknown_not_skipped(tmp_path, me):
    w = World(tmp_path, "r6feedbad", me)
    try:
        proc = w.start(0.01, 0.2)
        assert _wait(lambda: len(w.samples()) >= 2, 60), w.text()
        with open(w.feed_path, "a", encoding="ascii") as fh:
            fh.write("99.99.2026,25:61:00.000,20.0,99.0,40.0,No,No\n")
        code = proc.wait(timeout=90)
        assert code == 3 and w.receipt()["reason"].startswith("feed-unreadable"), (code, w.receipt(), w.text())
    finally:
        w.close()


def test_r6_c_A2_hot_rows_below_a_future_watermark_across_reads_are_a_regression(tmp_path, me):
    w = World(tmp_path, "r6watermark", me)
    try:
        proc = w.start(0.004, 0.2)
        assert _wait(lambda: len(w.samples()) >= 2, 60), w.text()
        w.feed.paused = True
        time.sleep(0.3)
        start = datetime.now()
        with open(w.feed_path, "a", encoding="ascii") as fh:
            fh.write(_feed_line(start + timedelta(seconds=25)))           # a cool row 25 s ahead: inside the 30 s skew
        k = 1
        while proc.poll() is None and k < 60:
            time.sleep(0.7)                                                # more than two polls: each pair lands in a later read
            now = datetime.now()
            with open(w.feed_path, "a", encoding="ascii") as fh:
                fh.write(_feed_line(now, 96.0) + _feed_line(now + timedelta(seconds=25)))
            k += 1
        code = proc.wait(timeout=90)
        assert code == 3 and w.receipt()["reason"].startswith("feed-time-regressed"), (code, w.receipt(), w.text())
    finally:
        w.close()


def _module_call(tmp_path: Path, body: str) -> str:
    script = tmp_path / "module-call.ps1"
    script.write_text(f"$ErrorActionPreference = 'Stop'\nImport-Module -Force '{MODULE}'\n{body}\n", encoding="ascii")
    out = _ps(["-File", str(script)])
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.strip()


def test_r6_c_A2_the_watermark_helper_refuses_any_non_monotonic_read(tmp_path):
    got = _module_call(tmp_path, "\n".join([
        "$t = [DateTime]::new(2026, 1, 5, 12, 0, 0, [DateTimeKind]::Utc)",
        "$a = Step-HeavySlotAbWatermark -Times @($t.AddSeconds(25), $t.AddSeconds(1)) -Watermark $null",
        "$b = Step-HeavySlotAbWatermark -Times @($t.AddSeconds(1), $t.AddSeconds(26)) -Watermark $t.AddSeconds(25)",
        "$c = Step-HeavySlotAbWatermark -Times @($t.AddSeconds(20), $t.AddSeconds(26), $t.AddSeconds(27)) -Watermark $t.AddSeconds(25)",
        "$d = Step-HeavySlotAbWatermark -Times @($t.AddSeconds(3)) -Watermark $t.AddSeconds(25)",
        "Write-Output ('{0}|{1}|{2}|{3}|{4}' -f $a.Regressed, $b.Regressed, $c.Regressed, (@($c.NewIndex) -join ','), $d.Regressed)",
    ]))
    assert got == "True|False|False|1,2|True", got


def test_r6_c_A6_a_retry_may_not_start_after_the_budget_minus_its_maximum_jitter(tmp_path):
    got = _module_call(tmp_path, "\n".join([
        "$g = [DateTime]::new(2026, 1, 5, 12, 0, 10, [DateTimeKind]::Utc)",
        "$late = Test-HeavySlotAbRetryMayStart -Now $g.AddMilliseconds(-10) -GiveUp $g -MaxJitterMs 40",      # 9.990 s into a 10 s budget
        "$early = Test-HeavySlotAbRetryMayStart -Now $g.AddMilliseconds(-50) -GiveUp $g -MaxJitterMs 40",
        "Write-Output ('{0}|{1}' -f $late, $early)",
    ]))
    assert got == "False|True", got
    assert "Test-HeavySlotAbRetryMayStart" in DRIVER.read_text(encoding="ascii"), "Set-Switch must ask the shared retry rule"


def test_r6_the_evaluator_refuses_a_window_dir_named_by_its_8dot3_short_name_under_the_heavy_slot_dir(tmp_path):
    import ctypes
    hs = tmp_path / "heavyslot-long-directory-name"
    (hs / "receipts").mkdir(parents=True)
    (hs / "ledger.jsonl").write_text("", encoding="ascii")
    win = hs / "window-long-directory-name"
    win.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(win), buf, 1024)
    short = buf.value if n else ""
    if not short or short.lower() == str(win).lower() or "~" not in short:
        pytest.skip("8.3 short names are not generated on this volume")
    out = _ps(["-File", str(EVALUATOR), "-WindowDir", short, "-HeavySlotDir", str(hs), "-SkipHeadroom"])
    assert out.returncode == 2 and "path-overlap" in out.stdout, (short, out.returncode, out.stdout, out.stderr)
