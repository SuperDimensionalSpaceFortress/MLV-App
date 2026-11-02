"""Runtime retirement check for doctrine card mlv-app/vendored-bridge-test-lacks-upstream-native-entry-pin.

Question: does the vendored bridge test that unittest actually runs survive a live process at the pid it
looks up? A static read of the test file cannot answer that (a pin can sit under `if False:`, be
overridden by a side_effect, be skipped, or be shadowed by a later definition of the same name), so this
RUNS the test. It extracts `tools/agent-bridge` at a git ref into a temp dir, forces
`core.win_process.native_process_entry` (and the names bound to it) to answer every pid with a live-process
dict, and runs exactly the one test through unittest's own loader (last class of that name, last method of
that name, with its skips).

While the test runs, a CODE-OBJECT tracer (sys.settrace and threading.settrace, around the whole run) watches
every frame whose code is `server._wrapper_process_entry`'s, however that frame was reached: the module
attribute, an alias captured at import (`from server import _wrapper_process_entry as f`), functools.partial,
a class attribute, a default argument. At each such frame's `return` event it compares the returned value with
the frame's own `native` local: the call took the fast path exactly when it returned the object the native
lookup gave it. No line number is consulted, so the layout of the source (`if native is not None: return
native` on one line or on two) cannot change the answer, and no module attribute is replaced, so no alias can
route around the observation. Tracing is applied to threads that already exist too (3.12+; older Pythons say
UNKNOWN if another thread is alive).

A second signal needs no tracing at all: the forced native entry records every call it receives, from any
thread. The forcing is a loader wrapper on core.win_process, so importlib.reload of it, or a purge and fresh
import, re-forces it. Any call of the forced entry during the test means a lookup consulted the native entry
unpinned: UNPINNED, even if the test swapped the trace function or ran the call on an untraced thread. Out of
the model: a call made in a separate interpreter the test launches (it is not this process, so nothing here
forces or sees it).

Verdict (stdout, exactly one line; exit code):
  PINNED   (0)  the test ran and passed, was not skipped, an observed call looked a pid up and fell through to
                the fallback at least once, no observed call took the fast path, and every such call returned a
                real entry: the test's own pin took effect.
  UNPINNED (1)  some observed call returned the native result (or the forced entry).
  UNKNOWN  (2)  anything else: git/import/timeout error, the test absent, skipped, failed for another
                reason, tracing replaced by the test, or no call observed. Never read UNKNOWN as fixed.

Trust: this EXECUTES the ref's own test code (as the test suite itself does). The verdict line carries a
per-run nonce that is handed to the child on stdin, not argv, so an accidental `print("PINNED")` or an early
SystemExit in the test file is never taken for a verdict. A test file engineered to forge this protocol by
introspection is outside the model: it could do anything else the interpreter allows, too.

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
import threading

AGENT_DIR = "tools/agent-bridge"
TEST_FILE = "test_server_wrapper_phase2.py"
SERVER_FILE = "server.py"
TEST_CLASS = "ServerWrapperPhase2Tests"
TEST_NAME = "test_server_wrapper_process_entry_prefers_pwsh_for_cim"
NATIVE_LOCAL = "native"
FORCED = "FORCED-LIVE-PID"
CHILD_FLAG = "--child"
VERDICT_CODE = {"PINNED": 0, "UNPINNED": 1, "UNKNOWN": 2}


def _child(agent_dir, nonce):
    import unittest

    def emit(text):
        # The only line the parent believes: it carries the nonce.
        print("RESULT %s %s" % (nonce, text))
        return VERDICT_CODE[text.split(" ")[0]]

    import importlib.abc
    import importlib.machinery

    sys.path.insert(0, agent_dir)
    forced_calls = []    # every call of the forced native entry, from any thread, through any alias

    def forced(pid):
        forced_calls.append(pid)
        return {"pid": pid, "parent_pid": 0, "name": "python.exe", "command_line": FORCED,
                "executable_path": None, "creation_date": None}

    # Force the REAL implementation at its source BEFORE anything imports it: server, the wrapper, the
    # trampoline and the test module all bind it by `from core.win_process import native_process_entry`,
    # and a pin whose side_effect delegates to the real function (the host answers None for an idle pid)
    # must reach the forced one, not a host-dependent answer. The forcing is a loader wrapper, so it is
    # re-applied by every (re)execution of core.win_process: importlib.reload(core.win_process), or a
    # purge from sys.modules and a fresh import, cannot restore the host-dependent function.
    class ForcingFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != "core.win_process":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                raise ImportError("cannot force core.win_process: unexpected loader")

            class ForcingLoader(type(spec.loader)):
                def exec_module(self, module):
                    super().exec_module(module)
                    module.native_process_entry = forced

            spec.loader = ForcingLoader(spec.loader.name, spec.loader.path)
            return spec

    finder = ForcingFinder()
    sys.meta_path.insert(0, finder)
    import core.win_process as win_process
    import server

    if win_process.native_process_entry is not forced or server.native_process_entry is not forced:
        return emit("UNKNOWN (could not force the native process entry)")
    import test_server_wrapper_phase2 as mod

    cls = getattr(mod, TEST_CLASS, None)
    if cls is None or not callable(getattr(cls, TEST_NAME, None)):
        return emit("UNKNOWN (the qualified test is absent; retired or renamed?)")
    # The code object is captured ONCE, from the loaded module, before the suite runs. Every alias the test
    # file can hold (import-time `from server import`, partial, class attribute, default argument) shares it.
    code = server._wrapper_process_entry.__code__
    if NATIVE_LOCAL not in code.co_varnames:
        return emit("UNKNOWN (server._wrapper_process_entry has no `%s` local; cannot tell the fast path)" % NATIVE_LOCAL)
    missing = object()
    frames = []    # one (kind, returned) per observed frame of the lookup, decided at its `return` event
    problems = []

    def local(frame, event, arg):
        if event == "return":
            try:
                native = frame.f_locals.get(NATIVE_LOCAL, missing)
                if native is missing:
                    kind = "nolookup"    # returned before the native lookup (e.g. not win32): proves nothing
                elif native is None:
                    kind = "lookup"      # the native lookup answered None: the fallback ran
                elif arg is native:
                    kind = "fast"        # returned exactly what the native lookup gave it
                else:
                    kind = "odd"
                frames.append((kind, arg))
            except BaseException as e:  # noqa: BLE001 -- an unreadable frame must not read as a pass
                problems.append("%s: %s" % (type(e).__name__, e))
        return local

    def tracer(frame, event, arg):
        c = frame.f_code
        return local if (c is code or c == code) else None

    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_CLASS + "." + TEST_NAME, mod)
    # Threads that already exist (a pool started at import) are traced too (3.12+); where that is not
    # available a second live thread cannot be observed, so the check says UNKNOWN rather than guess.
    set_threads = getattr(threading, "settrace_all_threads", None)
    if set_threads is None and threading.active_count() > 1:
        return emit("UNKNOWN (a thread other than the main one is alive and cannot be traced on this Python)")
    set_threads = set_threads or threading.settrace
    previous = sys.gettrace()
    forced_calls.clear()
    sys.settrace(tracer)
    set_threads(tracer)
    try:
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        disturbed = sys.gettrace() is not tracer or finder not in sys.meta_path
    finally:
        sys.settrace(previous)
        set_threads(None)
    if problems:
        return emit("UNKNOWN (tracer error: %s)" % problems[0])
    # Two independent signals that a lookup consulted the native entry unpinned: the traced frame returned
    # the object the native lookup gave it, or the forced native entry itself was CALLED (this one needs no
    # tracing, so a thread or a swapped trace function cannot hide it).
    if forced_calls or any(kind == "fast" for kind, _ in frames):
        return emit("UNPINNED (a lookup in the test consulted the native process entry)")
    if disturbed:
        return emit("UNKNOWN (the test replaced the trace function or the forcing finder; later calls were not observed)")
    if result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        return emit("UNKNOWN (the test was skipped or marked expected-failure; a skipped test is not retired)")
    if result.testsRun != 1 or not result.wasSuccessful():
        return emit("UNKNOWN (the test did not pass for a reason other than the fast path: run=%d errors=%d failures=%d)"
                    % (result.testsRun, len(result.errors), len(result.failures)))
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
    """Return (verdict line, exit code). Printing is main()'s job, after the temp dir is gone."""
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
    print(line)    # the one verdict line, after the temp dir is gone; its exit code is returned with it
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
