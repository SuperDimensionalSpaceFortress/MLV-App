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
     test does to tracing can hide it. The count is NEVER cleared.
  2. A sys.monitoring observer (Python 3.12+; interpreter-wide, so it covers every thread, and a test's own
     sys.settrace / threading.settrace calls do not touch it) watches every frame whose code is
     `server._wrapper_process_entry`'s, however that frame was reached (module attribute, an alias captured at
     import, functools.partial, a class attribute, a default argument, importlib.reload). At each frame's
     return it compares the returned value with the frame's own `native` local: the call took the fast path
     exactly when it returned the object the native lookup gave it. This catches a pin that answers with a
     real-looking dict. No line number is consulted, so `if native is not None: return native` on one line
     or on two cannot change the answer. This signal is also what proves a pinned lookup HAPPENED.

Both signals are armed BEFORE the test module is imported and stay armed until the verdict, so the whole life of
the test process is covered: import of the test module, construction of the TestCase, the run, and process
finalization (atexit callbacks the test registered, threads it left running). The verdict is computed and
printed by an atexit callback registered before any of the ref's code is imported; atexit runs LIFO, so it runs
after every callback the test registered, and after threading's shutdown joined the non-daemon threads. It then
ends the child with os._exit and the matching exit code.

Verdict (stdout, exactly one line; exit code):
  PINNED   (0)  the test ran and passed, was not skipped, a call made WHILE THE TEST RAN (not at import or
                construction) looked a pid up and fell through to the fallback at least once, no call at any
                time consulted the native entry, no thread other than the main one (threading or raw _thread)
                is still alive at exit, no exception was ignored at shutdown, and every observed fallback
                returned a real entry: the test's own pin took effect.
  UNPINNED (1)  some call, at any time, returned the native result, or the forced entry was called.
  UNKNOWN  (2)  anything else: git/import/timeout error, Python older than 3.12, the test absent, skipped, failed
                for another reason, the observer removed by the test, a thread still running at exit, a
                test-registered atexit callback that raised, the child printing after its verdict line, or no
                call observed. Never read UNKNOWN as fixed.

Trust: this EXECUTES the ref's own test code (as the test suite itself does). The verdict line carries a
per-run nonce that is handed to the child on stdin, not argv, so an accidental `print("PINNED")` or an early
SystemExit in the test file is never taken for a verdict. Out of the model: a test engineered to forge this
protocol by introspection, to tamper with sys.monitoring tool id 4, or to launch a separate interpreter (which
nothing here forces or sees) -- it could do anything else the interpreter allows, too.
The same holds for a test that replaces this checker's own mechanics: its streams, the result object's methods or
lists, or unittest internals. Bounded, not exhaustive: the verdict line goes out with os.write on a descriptor
duplicated before any ref code runs, nothing is flushed after it, the result is read once as plain data (exact
list/int types) and never called, every fact is re-read as the last step, and the checker reports UNKNOWN where it can
see the tampering (the result's fields replaced, unittest.TestCase._callTestMethod replaced while the test loaded or
no longer the wrapper installed here). A method that delegates the pinned lookup to a thread, which the method itself
starts or releases and the process does not outlive, is the method's own pin. Tampering that leaves no such trace is
out of model.

Usage: py -3 tools/coordination/check_bridge_native_pin.py [REF]        (REF defaults to fork/master)
       --test-source PATH / --server-source PATH replace the extracted test file / server.py (fixtures only).
"""
import atexit
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
SETTLE_SECONDS = 0.2
CHILD_FLAG = "--child"
VERDICT_CODE = {"PINNED": 0, "UNPINNED": 1, "UNKNOWN": 2}
# A child that raised from a test-registered atexit callback leaves one of these on stderr (3.12/3.13 and older).
ATEXIT_FAILURE_MARKERS = ("Exception ignored in atexit callback", "Error in atexit._run_exitfuncs")
FORCING_STUB = (
    "\n\n# --- appended by tools/coordination/check_bridge_native_pin.py (retirement check only):\n"
    "# every execution of this file yields the forced native entry.\n"
    "def native_process_entry(pid):\n"
    "    import builtins\n"
    "    return getattr(builtins, %r)(pid)\n" % FORCE_HOOK
)


def _child(agent_dir, st):
    """Arm everything, run the one test, and leave the verdict to _child_main's atexit callback (registered
    before any of the ref's code is imported). An early out sets st["early"] and returns."""
    import _thread
    import builtins
    import importlib
    import threading
    import time
    import unittest

    mon = getattr(sys, "monitoring", None)
    if mon is None:
        st["early"] = "UNKNOWN (needs Python 3.12+ for sys.monitoring; running %d.%d)" % sys.version_info[:2]
        return

    # Everything the verdict will use is captured HERE, before any of the ref's code runs, so a test that rebinds
    # time.sleep, _thread._count, sys.monitoring.get_tool or the like cannot make the verdict run its code.
    st["threading"] = threading
    st["sleep"] = time.sleep
    st["thread_count"] = _thread._count
    st["mon_get_tool"], st["mon_get_events"] = mon.get_tool, mon.get_events
    st["tamper"] = []
    test_case = st["TestCase"] = unittest.TestCase
    genuine_call = test_case.__dict__["_callTestMethod"]    # the raw function, read without any descriptor

    root = os.path.dirname(os.path.dirname(agent_dir))
    forced_calls = st["forced_calls"] = []    # every call of the forced native entry, any thread, any alias; NEVER cleared
    counting = [True]

    def forced(pid):
        if counting[0]:
            forced_calls.append(pid)
        return {"pid": pid, "parent_pid": 0, "name": "python.exe", "command_line": FORCED,
                "executable_path": None, "creation_date": None}

    setattr(builtins, FORCE_HOOK, forced)

    # Unraisable exceptions (a raising atexit callback is one) are recorded; a test that swaps the hook is seen.
    unraisable = st["unraisable"] = []
    previous_hook = sys.unraisablehook

    def hook(args):
        unraisable.append("%s: %s" % (getattr(args.exc_type, "__name__", "?"), args.exc_value))
        previous_hook(args)

    sys.unraisablehook = hook
    st["hook"] = hook

    # An exception that kills a thread goes through threading.excepthook, not the unraisable hook: recorded the same way.
    thread_errors = st["thread_errors"] = []
    previous_thread_hook = threading.excepthook

    def thread_hook(args):
        thread_errors.append("%s: %s" % (getattr(args.exc_type, "__name__", "?"), args.exc_value))
        previous_thread_hook(args)

    threading.excepthook = thread_hook
    st["thread_hook"] = thread_hook

    # The sys.monitoring observer is armed BEFORE any of the ref's code is imported: import, construction, run and
    # finalization are all inside it. Until the real code object is known (server imported) a frame is recognised
    # by its name and its `native` local; afterwards by equality, which an importlib.reload keeps.
    state = {"code": None}
    missing = object()
    # One (kind, returned, phase) per observed frame of the lookup, decided as the frame exits. The phase is the one
    # the frame STARTED in (PY_START), not the one it returns in: "load" (import, construction), "run" (unittest is
    # running the test: setUp, tearDown, cleanups), "method" (inside TestCase._callTestMethod, i.e. the test method
    # itself) or "done" (afterwards: finalization, test-created threads). Only a "method" lookup proves the test's
    # own pin took effect; a module-level, construction-time or setUp lookup cannot vouch for a method that was
    # shadowed by an empty one.
    phase = st["phase"] = ["load"]
    starts = {}    # monitored frame -> phase at its start
    frames = st["frames"] = []
    problems = st["problems"] = []

    def is_target(code_obj):
        known = state["code"]
        if known is None:
            return code_obj.co_name == "_wrapper_process_entry" and NATIVE_LOCAL in code_obj.co_varnames
        return code_obj is known or code_obj == known

    def observe(code_obj, returned):
        if not is_target(code_obj):
            return False
        try:
            frame = sys._getframe(2)    # callback <- observe <- the monitored frame's exit
            started = starts.pop(frame, "load")    # a frame whose start was not seen proves nothing
            native = frame.f_locals.get(NATIVE_LOCAL, missing)
            if frame.f_code is not code_obj:
                problems.append("monitored frame not found")
            elif native is missing:
                frames.append(("nolookup", returned, started))    # exited before the native lookup (e.g. not win32)
            elif native is None:
                frames.append(("lookup", returned, started))      # the native lookup answered None: the fallback ran
            elif returned is native:
                frames.append(("fast", returned, started))        # returned exactly what the native lookup gave it
            else:
                frames.append(("odd", returned, started))
        except BaseException as e:  # noqa: BLE001 -- an unreadable frame must not read as a pass
            problems.append("%s: %s" % (type(e).__name__, e))
        return True

    def on_return(code_obj, offset, retval):
        # DISABLE (this location, this tool) is allowed for PY_RETURN only; other code never fires again.
        return None if observe(code_obj, retval) else mon.DISABLE

    def on_unwind(code_obj, offset, exc):
        observe(code_obj, None)

    def on_start(code_obj, offset):
        if not is_target(code_obj):
            return mon.DISABLE
        starts[sys._getframe(1)] = phase[0]    # callback <- the monitored frame
        return None

    events = mon.events.PY_START | mon.events.PY_RETURN | mon.events.PY_UNWIND
    name = "bridge-native-pin"
    mon.use_tool_id(TOOL_ID, name)
    st["monitor"] = (mon, name, events)
    mon.register_callback(TOOL_ID, mon.events.PY_START, on_start)
    mon.register_callback(TOOL_ID, mon.events.PY_RETURN, on_return)
    mon.register_callback(TOOL_ID, mon.events.PY_UNWIND, on_unwind)
    mon.set_events(TOOL_ID, events)

    # `root` makes the CI identity importable; `agent_dir` is where the bridge's own top-level imports live
    # (the test file inserts it too).
    sys.path.insert(0, root)
    sys.path.insert(0, agent_dir)
    import core.win_process as win_process
    import server

    counting[0] = False    # the self-check below is ours, not the test's
    forcing_works = (win_process.native_process_entry(0).get("command_line") == FORCED
                     and server.native_process_entry(0).get("command_line") == FORCED)
    counting[0] = True
    if not forcing_works:
        st["early"] = "UNKNOWN (could not force the native process entry)"
        return
    # The code object is captured ONCE, before the test module is imported. Every alias the test file can hold
    # (import-time `from server import`, partial, class attribute, default argument) shares it, and a reloaded
    # server has an EQUAL one.
    code = server._wrapper_process_entry.__code__
    if NATIVE_LOCAL not in code.co_varnames:
        st["early"] = "UNKNOWN (server._wrapper_process_entry has no `%s` local; cannot tell the fast path)" % NATIVE_LOCAL
        return
    state["code"] = code

    mod = importlib.import_module(TEST_MODULE)    # import-time lookups are observed and counted
    cls = getattr(mod, TEST_CLASS, None)
    if cls is None or not callable(getattr(cls, TEST_NAME, None)):
        st["early"] = "UNKNOWN (the qualified test is absent; retired or renamed?)"
        return
    # Construction (the TestCase instance is built here) is observed and counted too; nothing is ever cleared.
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE + "." + TEST_CLASS + "." + TEST_NAME)
    # Tamper check: the phase boundary below is a patchable class attribute. If loading the test module already
    # replaced it, the "method" phase would be the test's own dispatcher, so the verdict is UNKNOWN. The same check
    # runs again at the end (the wrapper installed here must still be the one in place).
    original_call = test_case.__dict__["_callTestMethod"]
    if original_call is not genuine_call:
        st["tamper"].append("unittest.TestCase._callTestMethod was replaced while the test module loaded")

    def call_test_method(self, method):
        before = phase[0]
        phase[0] = "method"
        try:
            return original_call(self, method)
        finally:
            phase[0] = before

    st["wrapper"] = call_test_method
    test_case._callTestMethod = call_test_method
    phase[0] = "run"
    result = None
    try:
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        phase[0] = "done"
    # The result is read ONCE, here, as plain data (exact list/int/str types only, so nothing the test replaced on it
    # can run). The verdict later uses only this snapshot and never calls into the result.
    try:
        st["outcome"] = _snapshot(result)
    except TypeError as e:
        st["tamper"].append(str(e))


def _snapshot(result):
    def plain(name, kind):
        value = getattr(result, name)
        if type(value) is not kind:
            raise TypeError("the test replaced result.%s (%s)" % (name, type(value).__name__))
        return value

    errors, failures = plain("errors", list), plain("failures", list)
    why = ""
    for entry in errors + failures:
        if type(entry) is tuple and len(entry) == 2 and type(entry[1]) is str:
            why = (entry[1].strip().splitlines() or [""])[-1][:200]
        break
    return {"run": plain("testsRun", int), "errors": len(errors), "failures": len(failures), "why": why,
            "skipped": len(plain("skipped", list)), "expected_failures": len(plain("expectedFailures", list)),
            "unexpected_successes": len(plain("unexpectedSuccesses", list))}


def _verdict(st):
    """The verdict text, computed at the last moment the child process allows (an atexit callback run last)."""
    if st.get("early"):
        return st["early"]
    # The only step that lets other threads run. A raw thread started in the test's last statement may not have been
    # scheduled yet, and is not counted until it runs: give pending threads a moment to start (this releases the GIL).
    # Best effort, not a proof: a thread the host does not schedule within the pause is outside what this can see.
    # `_thread._count()` also counts threads started with _thread.start_new_thread, and is read without touching any
    # Thread object (a test's Thread subclass cannot run code here).
    st["sleep"](SETTLE_SECONDS)
    running = st["thread_count"]()
    return _decide(st, running)


def _decide(st, running):
    """Pure decision over data this checker owns. It calls nothing the test could have replaced, and it reads every
    fact (forced calls, observed frames, hooks, liveness) HERE, after every other inspection, as the last step."""
    threading = st["threading"]
    frames = st["frames"]
    if st["problems"]:
        return "UNKNOWN (observer error: %s)" % st["problems"][0]
    # A forced call or a fast-path return at ANY time (import, construction, run, a test-created thread or
    # atexit callback) is UNPINNED, whatever else is true.
    if st["forced_calls"] or any(kind == "fast" for kind, _, _ in frames):
        return "UNPINNED (a lookup in the test consulted the native process entry)"
    if running:
        return "UNKNOWN (a thread is still running at process exit: %d thread(s); a later lookup could not be seen)" % running
    if st["unraisable"] or sys.unraisablehook is not st["hook"]:
        return "UNKNOWN (an exception was ignored at shutdown, or the hook was replaced: %s)" % (
            st["unraisable"][0] if st["unraisable"] else "sys.unraisablehook replaced")
    if st["thread_errors"] or threading.excepthook is not st["thread_hook"]:
        return "UNKNOWN (a thread died with an exception, or threading.excepthook was replaced: %s)" % (
            st["thread_errors"][0] if st["thread_errors"] else "threading.excepthook replaced")
    mon, name, events = st["monitor"]
    if st["mon_get_tool"](TOOL_ID) != name or st["mon_get_events"](TOOL_ID) != events:
        return "UNKNOWN (the test replaced the observer; later calls were not observed)"
    # Tamper check: what this checker installed or captured is still what it was. A test that rewrites unittest's
    # internals is out of the model; it is reported as UNKNOWN where it can be seen.
    if st["tamper"]:
        return "UNKNOWN (tampering with the checker's own mechanics: %s)" % st["tamper"][0]
    if st["TestCase"].__dict__.get("_callTestMethod") is not st["wrapper"]:
        return "UNKNOWN (tampering with the checker's own mechanics: unittest.TestCase._callTestMethod is no longer the wrapper installed here)"
    out = st.get("outcome")
    if out is None:
        return "UNKNOWN (the test did not run to completion)"
    if out["skipped"] or out["expected_failures"] or out["unexpected_successes"]:
        return "UNKNOWN (the test was skipped or marked expected-failure; a skipped test is not retired)"
    if out["run"] != 1 or out["errors"] or out["failures"]:
        return ("UNKNOWN (the test did not pass for a reason other than the fast path: run=%d errors=%d failures=%d: %s)"
                % (out["run"], out["errors"], out["failures"], out["why"]))
    if any(kind == "odd" for kind, _, _ in frames):
        return "UNKNOWN (a call returned something other than the native result or the fallback's)"
    lookups = [(e, ph) for kind, e, ph in frames if kind == "lookup"]
    if not all(isinstance(e, dict) for e, _ in lookups):
        return "UNKNOWN (a pinned lookup did not return a real entry; nothing was proven)"
    if not any(ph == "method" for _, ph in lookups):
        return "UNKNOWN (no pinned lookup started inside the test method; a lookup at import, construction or setUp proves nothing about it)"
    return "PINNED"


def _child_main(agent_dir):
    nonce = sys.stdin.readline().strip()
    st = {}
    # Captured ONCE, before any of the ref's code runs: a private duplicate of the original stdout descriptor and the
    # os primitives that write to it and end the process. Nothing the test rebinds (sys.stdout, sys.__stdout__,
    # os.write, even fd 1) can reach the verdict line.
    out_fd = os.dup(1)
    os_write, os_exit = os.write, os._exit

    def emit(data):
        while data:
            data = data[os_write(out_fd, data):]

    def finish():
        # Registered with atexit before any of the ref's code runs, so atexit (LIFO) calls it after every callback
        # the test registered; threading's shutdown has already joined the non-daemon threads. It prints the only
        # line the parent believes (it carries the nonce) and ends the process with the matching exit code, so
        # nothing the interpreter does afterwards can change either.
        # Streams the TEST may have replaced are flushed BEFORE the verdict is computed: whatever their flush does
        # (a native lookup, an exception) is then seen by the verdict. After the verdict nothing the test controls
        # runs and nothing is flushed: the line goes out with os.write on the private descriptor, then os._exit.
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except BaseException as e:  # noqa: BLE001
                st.setdefault("problems", []).append("flushing %s failed: %s: %s" % (
                    "stdout" if stream is sys.stdout else "stderr", type(e).__name__, e))
        try:
            text = _verdict(st)
        except BaseException as e:  # noqa: BLE001
            text = "UNKNOWN (%s computing the verdict: %s)" % (type(e).__name__, e)
        code = VERDICT_CODE[text.split(" ")[0]]
        try:
            emit(("\nRESULT %s %s\n" % (nonce, text)).encode("utf-8", "replace"))    # leading newline: a test's unterminated print cannot hide it
        finally:
            os_exit(code)

    atexit.register(finish)
    try:
        _child(agent_dir, st)
    except BaseException as e:  # noqa: BLE001 -- an import error or an early SystemExit in the ref's code is UNKNOWN
        st["early"] = "UNKNOWN (%s: %s)" % (type(e).__name__, str(e).splitlines()[0] if str(e) else "")
    return VERDICT_CODE["UNKNOWN"]    # the exit code only if `finish` never ran; then there is no verdict line either


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
        verdict = verdicts[0]
        if verdict.split(" ")[0] == "PINNED":
            # A PINNED child is believed only if the process ended quietly: the verdict is the LAST thing it
            # printed (the child ends with os._exit right after it), and no atexit callback failed.
            after = proc.stdout.split(prefix + verdict, 1)[1] if (prefix + verdict) in proc.stdout else "?"
            if after.strip("\r\n") != "":
                return "UNKNOWN (the child printed after its verdict line)", 2
            if any(marker in proc.stderr for marker in ATEXIT_FAILURE_MARKERS):
                return "UNKNOWN (an atexit callback raised in the child)", 2
        return verdict, proc.returncode
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
