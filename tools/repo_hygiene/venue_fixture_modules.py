"""Scratch copies of the dual-venue PowerShell modules for the mutation fixtures (BACHELOR-THERMAL-GATE-WAIT-1 r14, CI-B1).

A mutation fixture copies one module (or the chain runner) into a temp dir with ONE line changed. Every sibling that file imports from its own directory
(`Import-Module (Join-Path $PSScriptRoot 'X.psm1')`, `$here` in the runner) must come along, or the import stops the run before the mutation under test and the
assertion fails for the wrong reason (hosted Tests run 41099141103: VenueChain.psm1 imports VenueRawJson.psm1). The sibling set is DERIVED from the source text,
transitively, never listed by hand, so the next sibling a module imports is copied without touching a fixture.

VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r5 (hosted Tests run 43580040634): a module may also READ a module of a neighbouring tools/profiling folder
(`Join-Path $PSScriptRoot '..\\bachelor\\AttrCudaArtifacts.psm1'`: VenueChain.psm1 reads the claim-shape predicate's text from it). Those are derived the same way
(cross_tree_modules) and copied to the same relative place beside the scratch dir, or the staged-manifest read fails before the behaviour under test.

VENUE-FIFO-ADVERSARY-TAIL-1 r8 (hosted run 45330079260 attempt 1: "windows unittest heavy 1/14" cancelled at the 50 min job cap, no test failed, its log lost; attempt 2
of the same head ran the part in 1652 s): run_tree_bounded / communicate_tree_bounded. On Windows subprocess.run(timeout=) is NOT a bound once the child has children:
on expiry CPython kills only the root and then calls communicate() with NO timeout (Lib/subprocess.py run(), the _mswindows branch), which waits for the LAST writer
of the captured pipes to exit. The runners these suites drive start pwsh children (the quiet probe, the legs, worker sleepers) that inherit those pipes, so one test over
its bound sat until a grandchild ended or the job cap cancelled the part, and the timeout's own failure (the case named, its output) never printed. Here a timeout kills
the WHOLE tree (taskkill /T, then the root), collects what the pipes hold within a bound, and raises the TimeoutExpired the caller already handles.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

# every spawn below carries the hidden-window flag (MLV-LANE-CONSOLE-HIDDEN-1: tools/repo_hygiene/test_hidden_spawn_flags.py scans this module): no_window_flags ORs
# CREATE_NO_WINDOW into a flag the caller already passes (CREATE_NEW_PROCESS_GROUP) instead of replacing it, as runtime_floor_suites does.
from .hidden_spawn import no_window_flags

# each cleanup step after a timeout is bounded too: the tree kill, the root's reap, and the read of what the pipes still hold
TREE_KILL_BOUND_SEC = 30.0
TREE_COLLECT_BOUND_SEC = 30.0


def kill_tree(proc: subprocess.Popen) -> str:
    """Kill `proc` and every process below it (taskkill /F /T, before the root dies: the tree is found through live parents), then the root; each wait is bounded.
    Returns '' when it went as planned, else what did not (a cleanup note never replaces the caller's verdict)."""
    problems: list[str] = []
    if os.name == "nt":
        try:
            tk = subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=no_window_flags())
            try:
                # r8 adversary A7 (1564db75): a non-zero taskkill (access denied, a root already gone) is a tree kill NOT confirmed, never a clean note
                code = tk.wait(timeout=TREE_KILL_BOUND_SEC)
                if code != 0:
                    problems.append(f"taskkill exited {code}")
            except subprocess.TimeoutExpired:
                tk.kill()
                problems.append(f"taskkill did not exit in {TREE_KILL_BOUND_SEC:g} s")
        except OSError as exc:
            problems.append(f"taskkill could not start: {exc}")
    try:
        proc.kill()
    except OSError:
        pass   # already gone
    try:
        proc.wait(timeout=TREE_KILL_BOUND_SEC)
    except subprocess.TimeoutExpired:
        problems.append(f"the root was still running {TREE_KILL_BOUND_SEC:g} s after its kill")
    return "; ".join(problems)


def communicate_tree_bounded(proc: subprocess.Popen, timeout: float, input: object = None) -> tuple:
    """proc.communicate(input, timeout=timeout), but on expiry the whole tree is killed (kill_tree) and what the pipes hold is read within TREE_COLLECT_BOUND_SEC
    (a process outside the tree that still holds a pipe is not waited for), then subprocess.TimeoutExpired is raised carrying that output."""
    try:
        return proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out, err = exc.output, exc.stderr
        note = kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=TREE_COLLECT_BOUND_SEC)
        except subprocess.TimeoutExpired:
            note = "; ".join(x for x in (note, f"a pipe was still held {TREE_COLLECT_BOUND_SEC:g} s after the tree kill") if x)
            # r8 adversary A8 (1564db75): a stream that DID reach its end is kept when only the other is still held. CPython's Windows communicate() reads each pipe whole on its
            # own thread into proc._stdout_buff / proc._stderr_buff (one item, at EOF) and raises with neither; a DrainedPopen keeps what it has read in .drained_output()
            if isinstance(proc, DrainedPopen):
                out, err = proc.drained_output()
            else:
                out = (getattr(proc, "_stdout_buff", None) or [out])[0]
                err = (getattr(proc, "_stderr_buff", None) or [err])[0]
        except (OSError, ValueError):
            pass   # the pipes are closed: nothing more to read
        expired = subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err)
        expired.tree_kill_note = note   # type: ignore[attr-defined]
        raise expired from None


class DrainedPopen:
    """A subprocess.Popen (held, not subclassed: test_hidden_spawn_flags REPORTS a spawn API as a base class, where its calls cannot be followed; every attribute not defined
    here is the held Popen's, so pid, args, stdin, stdout, returncode, poll, wait, kill, send_signal and the `with` form read as they do on a Popen) whose PIPE streams are read
    from the moment it starts, each on a daemon thread, so a child that writes more than a pipe holds (about 4 KB on Windows)
    never blocks on a write while the test waits on its progress (a batch.log line, a probe call) before it calls communicate().

    r8 (test_artifact_preflight_r5_l05_...: 9.8 s alone, 300 s under load in the r8 timing run): the runner wrote 6402 bytes to an undrained stdout pipe before and after the
    case's wait for three denied polls; past the pipe's size it stalled on a write, so the polls stopped and the case sat out its 120 s and 240 s bounds.
    communicate(input, timeout) waits for the process (raising subprocess.TimeoutExpired as Popen.communicate does), then for the readers within TREE_COLLECT_BOUND_SEC (a
    grandchild that still holds a pipe is not waited for), and returns what was read; a second call after a timeout returns the rest."""

    def __init__(self, *args, **kwargs) -> None:
        self._proc = subprocess.Popen(*args, creationflags=no_window_flags(kwargs.pop("creationflags", 0)), **kwargs)
        self._drain_bufs: dict[str, list] = {}
        self._drain_threads: list = []
        for name in ("stdout", "stderr"):
            stream = getattr(self._proc, name)
            if stream is not None:
                buf: list = []
                self._drain_bufs[name] = buf
                t = threading.Thread(target=self._drain, args=(stream, buf), daemon=True)
                t.start()
                self._drain_threads.append(t)

    def __getattr__(self, name: str):
        # only reached for a name this class does not define; `_proc` itself is never forwarded (an instance whose Popen did not start would recurse)
        if name == "_proc":
            raise AttributeError(name)
        return getattr(self._proc, name)

    def __enter__(self) -> "DrainedPopen":
        return self

    def __exit__(self, *exc_info) -> None:
        # Popen.__exit__ minus the pipe closes the drain threads own: close stdin, wait for the child
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        self._proc.wait()

    def __repr__(self) -> str:
        return f"DrainedPopen({self._proc!r})"

    @staticmethod
    def _drain(stream, buf: list) -> None:
        try:
            while True:
                # a line at a time (a text read(n) holds back what it read until n characters or EOF), so drained_output() after a timeout has every finished line
                chunk = stream.readline()
                if not chunk:
                    break
                buf.append(chunk)
        except (OSError, ValueError):
            return   # closed under the reader
        try:
            stream.close()   # EOF: the last writer is gone, nothing more can arrive
        except (OSError, ValueError):
            pass

    def drained_output(self) -> tuple:
        """(stdout, stderr) as read so far; None for a stream that is not a PIPE."""
        def joined(name: str):
            buf = self._drain_bufs.get(name)
            if buf is None:
                return None
            parts = list(buf)
            return (type(parts[0])() if parts else ("" if self._proc.text_mode else b"")).join(parts)
        return joined("stdout"), joined("stderr")

    def communicate(self, input=None, timeout=None):   # noqa: A002 (Popen's own parameter name)
        if input is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(input)
                self._proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        self._proc.wait(timeout=timeout)
        for t in self._drain_threads:
            t.join(TREE_COLLECT_BOUND_SEC)
        return self.drained_output()


def run_tree_bounded(*popenargs, input: object = None, capture_output: bool = False, timeout: float | None = None, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with the same arguments and result, whose `timeout` IS a bound on Windows (see the module doc): on expiry the whole tree is killed and
    subprocess.TimeoutExpired is raised within about timeout + 2 x TREE_KILL_BOUND_SEC + TREE_COLLECT_BOUND_SEC. With no timeout, or off Windows (POSIX run() reads
    the pipes itself and only waits for the killed root), it IS subprocess.run."""
    if timeout is None or os.name != "nt":
        return subprocess.run(*popenargs, input=input, capture_output=capture_output, timeout=timeout, check=check,
                              creationflags=no_window_flags(kwargs.pop("creationflags", 0)), **kwargs)
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(*popenargs, creationflags=no_window_flags(kwargs.pop("creationflags", 0)), **kwargs)
    try:
        out, err = communicate_tree_bounded(proc, timeout, input)
    except subprocess.TimeoutExpired:
        raise
    except BaseException:
        kill_tree(proc)
        raise
    code = proc.poll()
    if check and code:
        raise subprocess.CalledProcessError(code, proc.args, output=out, stderr=err)
    return subprocess.CompletedProcess(proc.args, code, out, err)

_SIBLING_IMPORT = re.compile(r"^\s*Import-Module\s+\(Join-Path\s+\$(?:PSScriptRoot|here)\s+'([^']+\.psm1)'\)", re.MULTILINE)
# a module of a NEIGHBOURING folder, by a relative path that climbs one level: '..\<folder>\<name>.psm1' (imported or read as text)
_CROSS_TREE_MODULE = re.compile(r"Join-Path\s+\$(?:PSScriptRoot|here)\s+'(\.\.\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+\.psm1)'")


def sibling_modules(source: Path) -> list[Path]:
    """The modules `source` imports from its own directory, transitively (source itself excluded), in first-seen order."""
    found: list[Path] = []
    pending = [source]
    while pending:
        current = pending.pop(0)
        for name in _SIBLING_IMPORT.findall(current.read_text(encoding="utf-8")):
            sibling = source.parent / name
            if sibling != source and sibling not in found:
                found.append(sibling)
                pending.append(sibling)
    return found


def cross_tree_modules(source: Path) -> list[str]:
    """The '..\\<folder>\\<name>.psm1' paths `source` and its siblings import or read, relative to `source`'s folder, in first-seen order."""
    found: list[str] = []
    for current in [source, *sibling_modules(source)]:
        for rel in _CROSS_TREE_MODULE.findall(current.read_text(encoding="utf-8")):
            if rel not in found:
                found.append(rel)
    return found


def copy_cross_tree_modules(source: Path, dest_dir: Path) -> list[Path]:
    """Copy every cross_tree_modules(source) file to the same path relative to `dest_dir` (a '..\\bachelor\\X.psm1' read lands in dest_dir.parent/bachelor). Returns the copies."""
    copies: list[Path] = []
    for rel in cross_tree_modules(source):
        parts = rel.split("\\")
        real = source.parent.joinpath(*parts).resolve()
        copy = dest_dir.joinpath(*parts).resolve()
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, copy)
        copies.append(copy)
    return copies


def copy_with_siblings(source: Path, dest_dir: Path, text: str | None = None) -> Path:
    """Copy `source` into `dest_dir` (its text replaced by `text` when given) with every sibling module it imports, byte for byte, and every
    neighbouring-folder module it reads (copy_cross_tree_modules: written beside `dest_dir`, so give it a dir of its own). Returns the copy of `source`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for sibling in sibling_modules(source):
        shutil.copy2(sibling, dest_dir / sibling.name)
    copy_cross_tree_modules(source, dest_dir)
    copy = dest_dir / source.name
    if text is None:
        shutil.copy2(source, copy)
    else:
        copy.write_text(text, encoding="utf-8")
    return copy
