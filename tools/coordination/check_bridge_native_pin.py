"""Runtime retirement check for doctrine card mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin.

Question: does the vendored bridge test that unittest actually runs survive a live process at the pid it
looks up? A static read of the test file cannot answer that (a pin can sit under `if False:`, be
overridden by a side_effect, be skipped, or be shadowed by a later definition of the same name), so this
RUNS the test. It extracts `tools/agent-bridge` at a git ref into a temp dir, forces
`core.win_process.native_process_entry` (and the names bound to it) to answer every pid with a live-process
dict, and runs exactly the one test through unittest's own loader (last class of that name, last method of
that name, with its skips). While it runs, a line tracer watches `server._wrapper_process_entry` and records
whether execution reached its fast-path `return native`: the ACTUAL call is observed, not a separate probe.

Verdict (stdout, exit code):
  PINNED   (0)  the test ran and passed, was not skipped, looked a pid up at least once, no lookup reached the
                fast-path return, and every lookup returned a real entry: the test's own pin took effect.
  UNPINNED (1)  a lookup in the test reached the fast-path return (or returned the forced entry).
  UNKNOWN  (2)  anything else: git/import/timeout error, the test absent, skipped, failed for another
                reason, or no lookup observed. Never read UNKNOWN as fixed.

Trust: this EXECUTES the ref's own test code (as the test suite itself does). The verdict line carries a
per-run nonce that is handed to the child on stdin, not argv, so an accidental `print("PINNED")` or an early
SystemExit in the test file is never taken for a verdict. A test file engineered to forge this protocol by
introspection is outside the model: it could do anything else the interpreter allows, too.

Usage: py -3 tools/coordination/check_bridge_native_pin.py [REF]        (REF defaults to fork/master)
       --test-source PATH replaces the extracted test file with PATH (fixtures only).
"""
import ast
import inspect
import io
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import textwrap

AGENT_DIR = "tools/agent-bridge"
TEST_FILE = "test_server_wrapper_phase2.py"
TEST_CLASS = "ServerWrapperPhase2Tests"
TEST_NAME = "test_server_wrapper_process_entry_prefers_pwsh_for_cim"
FORCED = "FORCED-LIVE-PID"
CHILD_FLAG = "--child"
VERDICT_CODE = {"PINNED": 0, "UNPINNED": 1, "UNKNOWN": 2}


def _fast_path_lines(func):
    """Absolute line numbers of `return native` inside func (the fast-path return), or []."""
    lines, start = inspect.getsourcelines(func)
    tree = ast.parse(textwrap.dedent("".join(lines)))
    return sorted(
        start + n.lineno - 1
        for n in ast.walk(tree)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == "native"
    )


def _child(agent_dir, nonce):
    import unittest

    def emit(text):
        # The only line the parent believes: it carries the nonce.
        print("RESULT %s %s" % (nonce, text))
        return VERDICT_CODE[text.split(" ")[0]]

    sys.path.insert(0, agent_dir)

    def forced(pid):
        return {"pid": pid, "parent_pid": 0, "name": "python.exe", "command_line": FORCED,
                "executable_path": None, "creation_date": None}

    # Force the REAL implementation at its source BEFORE anything imports it: server, the wrapper, the
    # trampoline and the test module all bind it by `from core.win_process import native_process_entry`,
    # and a pin whose side_effect delegates to the real function (the host answers None for an idle pid)
    # must reach the forced one, not a host-dependent answer. Forcing precedes the test module's import
    # so a pin that module sets at import time still wins.
    import core.win_process as win_process
    win_process.native_process_entry = forced
    import server

    server.native_process_entry = forced
    import test_server_wrapper_phase2 as mod

    cls = getattr(mod, TEST_CLASS, None)
    if cls is None or not callable(getattr(cls, TEST_NAME, None)):
        return emit("UNKNOWN (the qualified test is absent; retired or renamed?)")
    original = server._wrapper_process_entry
    fast_lines = _fast_path_lines(original)
    if not fast_lines:
        return emit("UNKNOWN (cannot locate the fast-path `return native` in server._wrapper_process_entry)")
    code = original.__code__
    seen = []
    reached = []

    def recording(pid):
        def local(frame, event, arg):
            if event == "line" and frame.f_lineno in fast_lines:
                reached.append(frame.f_lineno)
            return local

        def tracer(frame, event, arg):
            return local if frame.f_code is code else None

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            entry = original(pid)
        finally:
            sys.settrace(previous)
        seen.append(entry)
        return entry

    server._wrapper_process_entry = recording
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_CLASS + "." + TEST_NAME, mod)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    forced_seen = any(isinstance(e, dict) and e.get("command_line") == FORCED for e in seen)
    if reached or forced_seen:
        return emit("UNPINNED (a lookup in the test reached the native fast-path return)")
    if result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        return emit("UNKNOWN (the test was skipped or marked expected-failure; a skipped test is not retired)")
    if result.testsRun != 1 or not result.wasSuccessful():
        return emit("UNKNOWN (the test did not pass for a reason other than the fast path: run=%d errors=%d failures=%d)"
                    % (result.testsRun, len(result.errors), len(result.failures)))
    if not seen or not all(isinstance(e, dict) for e in seen):
        return emit("UNKNOWN (the test passed but no pid lookup returned a real entry; nothing was proven)")
    return emit("PINNED")


def _child_main(agent_dir):
    nonce = sys.stdin.readline().strip()
    try:
        return _child(agent_dir, nonce)
    except BaseException as e:  # noqa: BLE001 -- an import error or an early SystemExit in the ref's code is UNKNOWN
        print("RESULT %s UNKNOWN (%s: %s)" % (nonce, type(e).__name__, str(e).splitlines()[0] if str(e) else ""))
        return VERDICT_CODE["UNKNOWN"]


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
        if test_source is not None:
            with open(test_source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        nonce = secrets.token_hex(16)
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), CHILD_FLAG, agent_dir],
                              input=nonce + "\n", cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
        prefix = "RESULT %s " % nonce
        verdicts = [l[len(prefix):] for l in proc.stdout.splitlines() if l.startswith(prefix)]
        if len(verdicts) == 1 and verdicts[0].split(" ")[0] in VERDICT_CODE \
                and proc.returncode == VERDICT_CODE[verdicts[0].split(" ")[0]]:
            print(verdicts[0])
            return proc.returncode
        tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        print("UNKNOWN (the check run itself failed, exit %d: %s)" % (proc.returncode, tail))
        return 2


def main(argv):
    try:
        if len(argv) == 3 and argv[1] == CHILD_FLAG:
            return _child_main(argv[2])
        args = argv[1:]
        test_source = None
        if "--test-source" in args:
            i = args.index("--test-source")
            test_source = args[i + 1]
            args[i:i + 2] = []
        return _parent(args[0] if args else "fork/master", test_source)
    except Exception as e:  # noqa: BLE001 -- any failure to run the check is UNKNOWN, never a pass
        print("UNKNOWN (%s: %s)" % (type(e).__name__, str(e).splitlines()[0] if str(e) else ""))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
