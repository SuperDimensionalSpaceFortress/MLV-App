"""Runtime retirement check for doctrine card mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin.

Question: does the vendored bridge test that unittest actually runs survive a live process at the pid it
looks up? A static read of the test file cannot answer that (a pin can sit under `if False:`, be
overridden by a side_effect, be skipped, or be shadowed by a later definition of the same name), so this
RUNS the test. It extracts `tools/agent-bridge` at a git ref into a temp dir and forces
`core.win_process.native_process_entry` to answer every pid with a live-process dict: the FORCING IS WRITTEN
INTO THE EXTRACTED core/win_process.py itself (a stub appended to the file), so every execution of that file
-- import, importlib.reload, a purge and fresh import, a spec_from_file_location load under another name --
yields the forced function, never the host-dependent one. It then runs exactly the one test through
unittest's own loader under the identity CI uses (`tools.agent-bridge.test_server_wrapper_phase2.<class>.<name>`,
as the "Run agent bridge PowerShell launcher regressions" step of factory-bridge.yml does, so a skip that
depends on the module's package identity is seen): last class of that name, last method of that name,
with its skips.

Two independent signals mean "a lookup consulted the native entry unpinned":
  1. The forced entry records every call it receives, from any thread. No tracing is involved, so nothing the
     test does to tracing can hide it.
  2. A sys.monitoring observer (Python 3.12+; interpreter-wide, so it covers every thread, and a test's own
     sys.settrace / threading.settrace calls do not touch it) watches every frame whose code is
     `server._wrapper_process_entry`'s, however that frame was reached (module attribute, an alias captured at
     import, functools.partial, a class attribute, a default argument, importlib.reload). At each frame's
     return it compares the returned value with the frame's own `native` local: the call took the fast path
     exactly when it returned the object the native lookup gave it. This catches a pin that answers with a
     real-looking dict. No line number is consulted, so `if native is not None: return native` on one line
     or on two cannot change the answer. This signal is also what proves a pinned lookup HAPPENED.

Verdict (stdout, exactly one line; exit code):
  PINNED   (0)  the test ran and passed, was not skipped, an observed call looked a pid up and fell through to
                the fallback at least once, no call consulted the native entry, and every observed fallback
                returned a real entry: the test's own pin took effect.
  UNPINNED (1)  some call returned the native result, or the forced entry was called.
  UNKNOWN  (2)  anything else: git/import/timeout error, Python older than 3.12, the test absent, skipped, failed
                for another reason, the observer removed by the test, or no call observed. Never read UNKNOWN
                as fixed.

Trust: this EXECUTES the ref's own test code (as the test suite itself does). The verdict line carries a
per-run nonce that is handed to the child on stdin, not argv, so an accidental `print("PINNED")` or an early
SystemExit in the test file is never taken for a verdict. Out of the model: a test engineered to forge this
protocol by introspection, to tamper with sys.monitoring tool id 4, or to launch a separate interpreter (which
nothing here forces or sees) -- it could do anything else the interpreter allows, too.

Usage: py -3 tools/coordination/check_bridge_native_pin.py [REF]        (REF defaults to fork/master)
       --test-source PATH / --server-source PATH replace the extracted test file / server.py (fixtures only).
"""
import io
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile

AGENT_DIR = "tools/agent-bridge"
TEST_FILE = "test_server_wrapper_phase2.py"
SERVER_FILE = "server.py"
WIN_PROCESS_FILE = "core/win_process.py"
TEST_MODULE = "tools.agent-bridge.test_server_wrapper_phase2"
TEST_CLASS = "ServerWrapperPhase2Tests"
TEST_NAME = "test_server_wrapper_process_entry_prefers_pwsh_for_cim"
NATIVE_LOCAL = "native"
FORCED = "FORCED-LIVE-PID"
FORCE_HOOK = "_BRIDGE_NATIVE_PIN_FORCED"
TOOL_ID = 4
CHILD_FLAG = "--child"
VERDICT_CODE = {"PINNED": 0, "UNPINNED": 1, "UNKNOWN": 2}
FORCING_STUB = (
    "\n\n# --- appended by tools/coordination/check_bridge_native_pin.py (retirement check only):\n"
    "# every execution of this file yields the forced native entry.\n"
    "def native_process_entry(pid):\n"
    "    import builtins\n"
    "    return getattr(builtins, %r)(pid)\n" % FORCE_HOOK
)


def _child(agent_dir, nonce):
    import builtins
    import importlib
    import unittest

    def emit(text):
        # The only line the parent believes: it carries the nonce.
        print("RESULT %s %s" % (nonce, text))
        return VERDICT_CODE[text.split(" ")[0]]

    mon = getattr(sys, "monitoring", None)
    if mon is None:
        return emit("UNKNOWN (needs Python 3.12+ for sys.monitoring; running %d.%d)" % sys.version_info[:2])

    root = os.path.dirname(os.path.dirname(agent_dir))
    forced_calls = []    # every call of the forced native entry, from any thread, through any alias

    def forced(pid):
        forced_calls.append(pid)
        return {"pid": pid, "parent_pid": 0, "name": "python.exe", "command_line": FORCED,
                "executable_path": None, "creation_date": None}

    setattr(builtins, FORCE_HOOK, forced)
    # `root` makes the CI identity importable; `agent_dir` is where the bridge's own top-level imports live
    # (the test file inserts it too).
    sys.path.insert(0, root)
    sys.path.insert(0, agent_dir)
    import core.win_process as win_process
    import server

    if win_process.native_process_entry(0).get("command_line") != FORCED \
            or server.native_process_entry(0).get("command_line") != FORCED:
        return emit("UNKNOWN (could not force the native process entry)")
    mod = importlib.import_module(TEST_MODULE)

    cls = getattr(mod, TEST_CLASS, None)
    if cls is None or not callable(getattr(cls, TEST_NAME, None)):
        return emit("UNKNOWN (the qualified test is absent; retired or renamed?)")
    # The code object is captured ONCE, from the loaded module, before the suite runs. Every alias the test
    # file can hold (import-time `from server import`, partial, class attribute, default argument) shares it,
    # and a reloaded server has an EQUAL one.
    code = server._wrapper_process_entry.__code__
    if NATIVE_LOCAL not in code.co_varnames:
        return emit("UNKNOWN (server._wrapper_process_entry has no `%s` local; cannot tell the fast path)" % NATIVE_LOCAL)
    missing = object()
    frames = []    # one (kind, returned) per observed frame of the lookup, decided as the frame exits
    problems = []

    def observe(code_obj, returned):
        if not (code_obj is code or code_obj == code):
            return False
        try:
            frame = sys._getframe(2)    # callback <- observe <- the monitored frame's exit
            native = frame.f_locals.get(NATIVE_LOCAL, missing)
            if frame.f_code is not code_obj:
                problems.append("monitored frame not found")
            elif native is missing:
                frames.append(("nolookup", returned))    # exited before the native lookup (e.g. not win32)
            elif native is None:
                frames.append(("lookup", returned))      # the native lookup answered None: the fallback ran
            elif returned is native:
                frames.append(("fast", returned))        # returned exactly what the native lookup gave it
            else:
                frames.append(("odd", returned))
        except BaseException as e:  # noqa: BLE001 -- an unreadable frame must not read as a pass
            problems.append("%s: %s" % (type(e).__name__, e))
        return True

    def on_return(code_obj, offset, retval):
        # DISABLE (this location, this tool) is allowed for PY_RETURN only; other code never fires again.
        return None if observe(code_obj, retval) else mon.DISABLE

    def on_unwind(code_obj, offset, exc):
        observe(code_obj, None)

    events = mon.events.PY_RETURN | mon.events.PY_UNWIND
    name = "bridge-native-pin"
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE + "." + TEST_CLASS + "." + TEST_NAME)
    forced_calls.clear()
    mon.use_tool_id(TOOL_ID, name)
    try:
        mon.register_callback(TOOL_ID, mon.events.PY_RETURN, on_return)
        mon.register_callback(TOOL_ID, mon.events.PY_UNWIND, on_unwind)
        mon.set_events(TOOL_ID, events)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        disturbed = mon.get_tool(TOOL_ID) != name or mon.get_events(TOOL_ID) != events
    finally:
        mon.set_events(TOOL_ID, 0)
        mon.register_callback(TOOL_ID, mon.events.PY_RETURN, None)
        mon.register_callback(TOOL_ID, mon.events.PY_UNWIND, None)
        mon.free_tool_id(TOOL_ID)
    if problems:
        return emit("UNKNOWN (observer error: %s)" % problems[0])
    if forced_calls or any(kind == "fast" for kind, _ in frames):
        return emit("UNPINNED (a lookup in the test consulted the native process entry)")
    if disturbed:
        return emit("UNKNOWN (the test replaced the observer; later calls were not observed)")
    if result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        return emit("UNKNOWN (the test was skipped or marked expected-failure; a skipped test is not retired)")
    if result.testsRun != 1 or not result.wasSuccessful():
        why = (result.errors + result.failures or [("", "")])[0][1].strip().splitlines()[-1:] or [""]
        return emit("UNKNOWN (the test did not pass for a reason other than the fast path: run=%d errors=%d failures=%d: %s)"
                    % (result.testsRun, len(result.errors), len(result.failures), why[0][:200]))
    if any(kind == "odd" for kind, _ in frames):
        return emit("UNKNOWN (a call returned something other than the native result or the fallback's)")
    lookups = [e for kind, e in frames if kind == "lookup"]
    if not lookups or not all(isinstance(e, dict) for e in lookups):
        return emit("UNKNOWN (the test passed but no observed lookup returned a real entry; nothing was proven)")
    return emit("PINNED")


def _child_main(agent_dir):
    nonce = sys.stdin.readline().strip()
    try:
        return _child(agent_dir, nonce)
    except BaseException as e:  # noqa: BLE001 -- an import error or an early SystemExit in the ref's code is UNKNOWN
        print("RESULT %s UNKNOWN (%s: %s)" % (nonce, type(e).__name__, str(e).splitlines()[0] if str(e) else ""))
        return VERDICT_CODE["UNKNOWN"]


def _replace(path, source):
    with open(source, "rb") as src, open(path, "wb") as dst:
        dst.write(src.read())


def _parent(ref, test_source, server_source):
    """Return (verdict line, exit code). Printing is main()'s job, after cleanup has been attempted."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    data = subprocess.check_output(["git", "archive", ref, AGENT_DIR], env=env)
    kwargs = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
    with tempfile.TemporaryDirectory(**kwargs) as tmp:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:
                tar.extractall(tmp)
        agent_dir = os.path.join(tmp, *AGENT_DIR.split("/"))
        target = os.path.join(agent_dir, TEST_FILE)
        if not os.path.isfile(target):
            return "UNKNOWN (%s is not in %s at %s; retired?)" % (TEST_FILE, AGENT_DIR, ref), 2
        if test_source is not None:
            _replace(target, test_source)
        if server_source is not None:
            _replace(os.path.join(agent_dir, SERVER_FILE), server_source)
        native_file = os.path.join(agent_dir, *WIN_PROCESS_FILE.split("/"))
        if not os.path.isfile(native_file):
            return "UNKNOWN (%s is not in %s at %s)" % (WIN_PROCESS_FILE, AGENT_DIR, ref), 2
        with open(native_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(FORCING_STUB)
        nonce = secrets.token_hex(16)
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), CHILD_FLAG, agent_dir],
                              input=nonce + "\n", cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
    prefix = "RESULT %s " % nonce
    verdicts = [l[len(prefix):] for l in proc.stdout.splitlines() if l.startswith(prefix)]
    if len(verdicts) == 1 and verdicts[0].split(" ")[0] in VERDICT_CODE \
            and proc.returncode == VERDICT_CODE[verdicts[0].split(" ")[0]]:
        return verdicts[0], proc.returncode
    tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
    return "UNKNOWN (the check run itself failed, exit %d: %s)" % (proc.returncode, tail), 2


def _take(args, flag):
    if flag not in args:
        return None
    i = args.index(flag)
    value = args[i + 1]
    args[i:i + 2] = []
    return value


def main(argv):
    try:
        if len(argv) == 3 and argv[1] == CHILD_FLAG:
            return _child_main(argv[2])
        args = argv[1:]
        test_source = _take(args, "--test-source")
        server_source = _take(args, "--server-source")
        line, code = _parent(args[0] if args else "fork/master", test_source, server_source)
    except Exception as e:  # noqa: BLE001 -- any failure to run the check is UNKNOWN, never a pass
        line, code = "UNKNOWN (%s: %s)" % (type(e).__name__, str(e).splitlines()[0] if str(e) else ""), 2
    print(line)    # the one verdict line, after the temp dir cleanup was attempted; its exit code goes with it
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
