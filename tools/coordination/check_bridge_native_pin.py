"""Runtime retirement check for doctrine card mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin.

Question: does the vendored bridge test that unittest actually runs survive a live process at the pid it
looks up? A static read of the test file cannot answer that (a pin can sit under `if False:`, be
overridden by a side_effect, be skipped, or be shadowed by a later definition of the same name), so this
RUNS the test. It extracts `tools/agent-bridge` at a git ref into a temp dir, forces
`server.native_process_entry` to answer every pid with a live-process dict, wraps
`server._wrapper_process_entry` to record what each lookup returned, and runs exactly the one test
through unittest's own loader (last class of that name, last method of that name, with its skips).

Verdict (stdout, exit code):
  PINNED   (0)  the test ran and passed, was not skipped, looked a pid up at least once, and every
                lookup saw the fast path answer None (not the forced entry): the test's own pin took effect.
  UNPINNED (1)  a lookup in the test was answered by the forced native entry: the fast path was reached.
  UNKNOWN  (2)  anything else: git/import/timeout error, the test absent, skipped, failed for another
                reason, or no lookup observed. Never read UNKNOWN as fixed.

Usage: py -3 tools/coordination/check_bridge_native_pin.py [REF]        (REF defaults to fork/master)
       --test-source PATH replaces the extracted test file with PATH (fixtures only).
"""
import io
import os
import subprocess
import sys
import tarfile
import tempfile

AGENT_DIR = "tools/agent-bridge"
TEST_FILE = "test_server_wrapper_phase2.py"
TEST_CLASS = "ServerWrapperPhase2Tests"
TEST_NAME = "test_server_wrapper_process_entry_prefers_pwsh_for_cim"
FORCED = "FORCED-LIVE-PID"
CHILD_FLAG = "--child"


def _child(agent_dir):
    import unittest

    sys.path.insert(0, agent_dir)
    import server

    # Force BEFORE the test module is imported, so a pin the module sets at import time still wins.
    server.native_process_entry = lambda pid: {
        "pid": pid, "parent_pid": 0, "name": "python.exe", "command_line": FORCED,
        "executable_path": None, "creation_date": None,
    }
    import test_server_wrapper_phase2 as mod

    cls = getattr(mod, TEST_CLASS, None)
    if cls is None or not callable(getattr(cls, TEST_NAME, None)):
        print("UNKNOWN (the qualified test is absent; retired or renamed?)")
        return 2
    seen = []
    probes = []
    original = server._wrapper_process_entry

    def recording(pid):
        # What the fast path would answer for this pid, asked in the test's own patch context.
        probes.append(server.native_process_entry(pid))
        entry = original(pid)
        seen.append(entry)
        return entry

    server._wrapper_process_entry = recording
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_CLASS + "." + TEST_NAME, mod)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    forced_seen = any(isinstance(e, dict) and e.get("command_line") == FORCED for e in seen + probes)
    if forced_seen:
        print("UNPINNED (a lookup in the test was answered by the native fast path)")
        return 1
    if result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        print("UNKNOWN (the test was skipped or marked expected-failure; a skipped test is not retired)")
        return 2
    if result.testsRun != 1 or not result.wasSuccessful():
        print("UNKNOWN (the test did not pass for a reason other than the fast path: run=%d errors=%d failures=%d)"
              % (result.testsRun, len(result.errors), len(result.failures)))
        return 2
    if not seen or not all(isinstance(e, dict) for e in seen) or any(p is not None for p in probes):
        print("UNKNOWN (the test passed but no pid lookup ran past a fast path that answers None; nothing was proven)")
        return 2
    print("PINNED")
    return 0


def _parent(ref, test_source):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    data = subprocess.check_output(["git", "archive", ref, AGENT_DIR], env=env)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:
                tar.extractall(tmp)
        agent_dir = os.path.join(tmp, *AGENT_DIR.split("/"))
        target = os.path.join(agent_dir, TEST_FILE)
        if not os.path.isfile(target):
            print("UNKNOWN (%s is not in %s at %s; retired?)" % (TEST_FILE, AGENT_DIR, ref))
            return 2
        if test_source:
            with open(test_source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), CHILD_FLAG, agent_dir],
                              cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
        out = proc.stdout.strip().splitlines()
        if out and out[-1].split(" ")[0] in ("PINNED", "UNPINNED", "UNKNOWN") and proc.returncode in (0, 1, 2):
            print(out[-1])
            return proc.returncode
        tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        print("UNKNOWN (the check run itself failed, exit %d: %s)" % (proc.returncode, tail))
        return 2


def main(argv):
    if len(argv) == 3 and argv[1] == CHILD_FLAG:
        return _child(argv[2])
    args = argv[1:]
    test_source = None
    if "--test-source" in args:
        i = args.index("--test-source")
        test_source = args[i + 1]
        del args[i:i + 2]
    try:
        return _parent(args[0] if args else "fork/master", test_source)
    except Exception as e:  # noqa: BLE001 -- any failure to run the check is UNKNOWN, never a pass
        print("UNKNOWN (%s: %s)" % (type(e).__name__, str(e).splitlines()[0] if str(e) else ""))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
