"""Executable check for the doctrine card unlocked-action-list-rewrite-drops-a-just-armed-action.

Two writers share one JSON action list. The beat reads the list, works, then writes its copy
back through a temp file and a rename. The arming command appends one uniquely named entry.
Each trial forces the worst interleaving: the arming command runs to completion between the
beat's read and the beat's write. The check passes only when

  1. no armed entry is missing at the end of any of the trials, and
  2. an arming command whose entry is erased right after its own write exits non-zero.

Two reference writer sets ship with the check so it is RED and GREEN on its own:

  py -3 action_list_lost_update_check.py --impl naive    -> RED   (exit 1): unlocked read-modify-write
  py -3 action_list_lost_update_check.py --impl locked   -> GREEN (exit 0): lock, fresh re-read, post-write verify
  py -3 action_list_lost_update_check.py --impl-module <file.py>

A module under test supplies  arm(path, entry_id, after_write=None) -> int  (the arming command:
returns its exit code; after_write is called once right after the write, for fault injection) and
beat_save(path, snapshot, changed) -> None  (the beat's write: snapshot is the list it read earlier,
changed maps id -> the row the beat changed).

Exit: 0 GREEN, 1 RED (the trap is present), 2 harness or usage error.
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

SEED_ROWS = 20


def read_list(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_atomic(path, rows):
    tmp = f"{path}.{threading.get_ident()}.tmp"
    Path(tmp).write_text(json.dumps(rows), encoding="utf-8")
    os.replace(tmp, path)  # atomic against a torn file, not against a lost update


class Naive:
    """The behaviour the trap describes: a read-modify-write with no shared lock."""

    @staticmethod
    def arm(path, entry_id, after_write=None):
        rows = read_list(path)
        rows.append({"id": entry_id, "status": "armed"})
        write_atomic(path, rows)
        if after_write:
            after_write()
        return 0  # the write succeeded, so the command reports success

    @staticmethod
    def beat_save(path, snapshot, changed):
        write_atomic(path, [changed.get(r["id"], r) for r in snapshot])


class Locked:
    """The fix: one lock around a fresh read and its write; the arming command proves its own entry."""

    @staticmethod
    def _lock(path):
        lock = f"{path}.lock"
        while True:
            try:
                return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY), lock
            except FileExistsError:
                time.sleep(0.005)

    @classmethod
    def _locked(cls, path, body):
        fd, lock = cls._lock(path)
        try:
            return body()
        finally:
            os.close(fd)
            os.unlink(lock)

    @classmethod
    def arm(cls, path, entry_id, after_write=None):
        def body():
            rows = [r for r in read_list(path) if r["id"] != entry_id]
            rows.append({"id": entry_id, "status": "armed"})
            write_atomic(path, rows)
            if after_write:
                after_write()
            return 0 if any(r["id"] == entry_id for r in read_list(path)) else 1  # fail loudly

        return cls._locked(path, body)

    @classmethod
    def beat_save(cls, path, snapshot, changed):
        def body():
            fresh = read_list(path)  # not the snapshot: an add may have landed meanwhile
            write_atomic(path, [changed.get(r["id"], r) for r in fresh])

        cls._locked(path, body)


def load_module(path):
    spec = importlib.util.spec_from_file_location("writers_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_trial(impl, workdir, n):
    path = str(Path(workdir) / f"actions-{n}.json")
    write_atomic(path, [{"id": f"seed{i}", "status": "done"} for i in range(SEED_ROWS)])
    entry = f"armed-{n}"
    beat_read, arm_done = threading.Event(), threading.Event()
    result = {}

    def beat():
        snapshot = read_list(path)  # the beat's read, taken before the add
        beat_read.set()
        arm_done.wait(10)  # the beat "works for some seconds"
        impl.beat_save(path, snapshot, {"seed0": {"id": "seed0", "status": "fired"}})

    def arm():
        beat_read.wait(10)
        result["exit"] = impl.arm(path, entry)
        arm_done.set()

    threads = [threading.Thread(target=beat), threading.Thread(target=arm)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    present = any(r["id"] == entry for r in read_list(path))
    return present, result.get("exit")


def forced_loss_exit(impl, workdir):
    path = str(Path(workdir) / "actions-forced.json")
    write_atomic(path, [{"id": f"seed{i}", "status": "done"} for i in range(SEED_ROWS)])

    def erase():
        write_atomic(path, [r for r in read_list(path) if r["id"] != "armed-forced"])

    return impl.arm(path, "armed-forced", after_write=erase)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--impl", choices=("naive", "locked"))
    group.add_argument("--impl-module")
    parser.add_argument("--trials", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        impl = load_module(args.impl_module) if args.impl_module else {"naive": Naive, "locked": Locked}[args.impl]
        with tempfile.TemporaryDirectory() as workdir:
            lost = silent = 0
            for n in range(args.trials):
                present, code = one_trial(impl, workdir, n)
                lost += not present
                silent += (not present) and code == 0
            forced = forced_loss_exit(impl, workdir)
    except Exception as exc:  # a harness failure must never read as GREEN
        print(f"ERROR harness could not run the writers: {exc!r}")
        return 2
    label = args.impl or "impl-module"
    print(f"{label}: lost={lost}/{args.trials} lost_but_arm_exit_0={silent}/{args.trials} forced_loss_arm_exit={forced}")
    if lost or forced == 0:
        print(f"RED {label}: the trap is present")
        return 1
    print(f"GREEN {label}: no armed entry lost; a forced loss exits non-zero")
    return 0


if __name__ == "__main__":
    sys.exit(main())
