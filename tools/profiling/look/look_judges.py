#!/usr/bin/env python3
"""Judge-runner interface for blind model judging (DESIGN.md AMENDMENT 2 B4). Pure standard library.

RUNNERS
    ClaudeCliJudge   a Claude lane: `claude -p` in a throw-away directory that holds ONLY the one pair image,
                     with the Read tool (Read renders a PNG to the model). Headless, one bounded process per item.
    CodexExecJudge   a Codex lane: `codex exec --image <png>` (read-only sandbox). probe_codex_image_support()
                     reads `codex exec --help` for the flag; probe_codex_vision() proves it LIVE on a synthetic
                     image (so "the flag exists" is never reported as "Codex can see").
    CallableJudge    wraps a function; used by tests and by any lane that judges out of process.

CROSS-FAMILY (K6): select_second_judge() returns a Codex judge only when the image probe passed; otherwise it
returns a second Claude model and the status CROSS_FAMILY_UNAVAILABLE, which look_tally copies into the entry.

JUDGES ARE NEVER THE PRODUCER OR THE HUB: assert_not_producer() refuses a judge whose model FAMILY matches the
family of a producer/hub model the caller names. Both sides go through ONE alias table (opus / sonnet / fable /
haiku <-> claude-<family>-<version>, any date suffix, any [1m]-style tag), so `sonnet` and `claude-sonnet-5-5`
are the same thing and any version of the producer's family is refused. A name that cannot be placed in a family
is refused too (fail closed), and the Codex default resolves to the real model name in config.toml.

BOUNDED: every judge or probe subprocess runs through run_bounded(), which kills the WHOLE process tree on timeout
(taskkill /T /F on Windows, where `claude` and `codex` are npm .cmd shims and a plain kill reaches only cmd.exe;
a process group elsewhere), so a hung judge cannot outlive its timeout.

BLINDING: each item runs in its own temp directory containing only `pair.png`; the prompt is the frozen rubric
plus a fixed instruction. Nothing about subjects, backends, flavors or seeds is ever in a prompt or a path.

ENFORCED ISOLATION (round 2): a judge CLI is a general agent, so flags that ask it not to read are not enough.
    1. The secrets are not on disk in the clear: build-session seals the answer key, the full session record, the
       source frames and the degraded sources into one file (look_seal.py) under a per-session key the judge never
       receives. The shipped runners REFUSE a session directory that is not sealed or still holds a plaintext secret.
    2. The key is scrubbed from the judge's environment (and the judge commands refuse to start with it set).
    3. The Codex judge has its tools switched off (shell, unified exec, image viewer, code mode, browser, apps,
       plugins, sub-agents ...): it can only look at the attached picture. The Claude judge keeps the Read tool,
       confined to its working directory by --restricted, and any permission denial rejects the verdict.
    4. `look_cli.py isolation-canary` proves 1-3 LIVE against a canary outside the scratch dir, with a control run
       that shows the canary IS readable when the confinement is removed.

STALE RESULTS: run_session re-judges any item whose stored verdict was made against a different image digest or a
different rubric digest, refuses a results file that belongs to another judge identity, and refuses to start
when the images on disk no longer match the session's recorded digests.
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import look_config
import look_seal

CROSS_FAMILY_UNAVAILABLE = "CROSS_FAMILY_UNAVAILABLE"
CROSS_FAMILY_PROVEN = "CROSS_FAMILY_PROVEN_LIVE"
CROSS_FAMILY_FLAG_ONLY = "CROSS_FAMILY_FLAG_PRESENT_NOT_PROVEN"

INSTRUCTION_CLAUDE = (
    "You are a blind judge of colour grading. Follow the frozen rubric below exactly.\n"
    "The image to judge is the file `pair.png` in your current working directory. Open it with your Read "
    "tool, look at it, and judge only what you see.\n"
    "Reply with ONE JSON object in the rubric's answer format and nothing else.\n"
)
INSTRUCTION_CODEX = (
    "You are a blind judge of colour grading. Follow the frozen rubric below exactly.\n"
    "The image to judge is attached to this message. Look at it and judge only what you see. Do not run "
    "commands or read files.\n"
    "Reply with ONE JSON object in the rubric's answer format and nothing else.\n"
)


class ProducerJudgeError(ValueError):
    pass


class JudgeError(RuntimeError):
    pass


# Codex features switched off for the judge: every one that gives the model a way to read, run or reach anything but
# the attached picture. Proven live (look_cli isolation-canary): with these off the model reports it has no shell and no
# image viewer, and with them on (the canary's control run) it reads a canary outside its directory.
CODEX_DISABLED_FEATURES = ("shell_tool", "unified_exec", "view_image", "code_mode_host", "browser_use", "computer_use",
                           "apps", "plugins", "skill_search", "hooks", "memories", "multi_agent", "multi_agent_v2",
                           "goals", "sleep_tool")

ANTHROPIC_FAMILIES = ("opus", "sonnet", "fable", "haiku")
CODEX_DEFAULT_MODEL = "codex-default"  # recorded only when config.toml names no model: the real model is UNKNOWN


def model_family_key(model):
    """THE alias table. Canonical family of a model name, or None when the name cannot be placed.

        ('anthropic', 'sonnet')        sonnet, Sonnet 5.5, claude-sonnet-5-5, claude-sonnet-5-5-20261001,
                                       us.anthropic.claude-sonnet-5-5-v1:0, sonnet[1m] ...
        ('openai', root, trim)         gpt-6.1-sol -> ('openai', 'gpt', 'sol'); gpt-5.5 -> ('openai', 'gpt', '');
                                       o3 -> ('openai', 'o', ''); codex-mini -> ('openai', 'codex', 'mini')
        ('openai', '*', '')            `codex`, `openai`, `codex-default` (the lane's unresolved default): any OpenAI model
    """
    text = re.sub(r"\[[^\]]*\]", "", (model or "").strip().lower())
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    if not tokens:
        return None
    families = {t for t in tokens if t in ANTHROPIC_FAMILIES}
    if len(families) == 1 and not any(t in ("gpt", "codex", "openai") for t in tokens):
        return ("anthropic", next(iter(families)))
    if families:
        return None  # two families in one name, or a Claude family beside an OpenAI token: ambiguous
    if tokens == ["claude"]:
        return None  # a Claude model with no family cannot be compared
    if tokens[0] in ("openai", "chatgpt") or tokens == ["codex"] or tokens[:2] == ["codex", "default"]:
        return ("openai", "*", "")
    root = None
    if tokens[0].startswith("gpt"):
        root = "gpt"
    elif re.fullmatch(r"o\d+", tokens[0]):
        root = "o"
    elif tokens[0] == "codex":
        root = "codex"
    if root is None:
        return None
    trim = next((t for t in tokens[1:] if t.isalpha()), "")
    return ("openai", root, trim)


def family_of(model):
    key = model_family_key(model)
    return "unknown" if key is None else key[0]


def _same_family(a, b):
    if a[0] != b[0]:
        return False
    if a[0] == "anthropic":
        return a[1] == b[1]
    if "*" in (a[1], b[1]):
        return True
    return a[1] == b[1] and (a[2] == b[2] or "" in (a[2], b[2]))


def assert_not_producer(judge_model, forbidden_models):
    """Raise ProducerJudgeError when the judge's model FAMILY is the family of a producer or hub model the caller
    lists. Fails closed: an unnamed judge, an unrecognised judge or producer name, and an empty forbidden list all
    raise, because none of them can prove independence."""
    if not (judge_model or "").strip():
        raise ProducerJudgeError("a judge must name its model")
    forbidden = [b for b in (forbidden_models or ()) if (b or "").strip()]
    if not forbidden:
        raise ProducerJudgeError("no producer/hub model was named, so the judge's independence cannot be checked")
    judge_key = model_family_key(judge_model)
    if judge_key is None:
        raise ProducerJudgeError(
            f"judge model {judge_model!r} cannot be placed in a model family; use opus/sonnet/fable/haiku, "
            "claude-<family>-<version> or a gpt/o/codex model name")
    for bad in forbidden:
        bad_key = model_family_key(bad)
        if bad_key is None:
            raise ProducerJudgeError(f"producer/hub model {bad!r} cannot be placed in a model family")
        if _same_family(judge_key, bad_key):
            raise ProducerJudgeError(
                f"judge model {judge_model!r} is in the family of the producer/hub model {bad!r} ({judge_key})")


def resolve_codex_default_model(codex_home=None):
    """The model `codex exec` uses when -m is absent: the top-level `model = "..."` of config.toml, else None."""
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        with open(os.path.join(home, "config.toml"), "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("["):
                    break  # a table header: top-level keys are over
                match = re.match(r'model\s*=\s*"([^"]+)"\s*(#.*)?$', stripped)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def _kill_tree(proc):
    """Kill proc AND everything it started. On Windows the claude/codex CLIs are npm .cmd shims: killing cmd.exe
    leaves the real CLI alive holding our pipes, so the tree must go."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def judge_environment(environ=None):
    """The environment a judge process gets: the caller's, minus the seal key and every other LOOK_* variable. The key
    is the one thing that turns sealed.bin back into the answer key, so a judge must never inherit it."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k != look_seal.KEY_ENV and not k.upper().startswith("LOOK_")}


def assert_no_seal_key_in_environment(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get(look_seal.KEY_ENV):
        raise JudgeError(f"{look_seal.KEY_ENV} is set in the environment of the judging process: a judge must not be "
                         "started where the seal key is reachable; unset it and pass the key only to `tally`")


def run_bounded(argv, input_text=None, cwd=None, timeout=300, grace_s=5, env=None):
    """subprocess.run replacement whose timeout covers the whole process tree. Returns a CompletedProcess; raises
    subprocess.TimeoutExpired (after killing the tree) so callers keep their existing except clause. Never waits
    longer than ~timeout + grace_s, even when a grandchild still holds the pipes. `env` replaces the environment."""
    kwargs = {"stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
              "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": cwd, "env": env,
              "text": True, "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=grace_s)
        except subprocess.TimeoutExpired:
            out = err = ""  # a survivor still holds a pipe; its reader threads are daemons, so we just leave
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def build_prompt(rubric_text, codex=False):
    return (INSTRUCTION_CODEX if codex else INSTRUCTION_CLAUDE) + "\n" + rubric_text


def extract_json_object(text):
    """Last balanced top-level {...} in text that parses as JSON; raises JudgeError when there is none."""
    candidates = []
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
    for blob in reversed(candidates):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise JudgeError("no JSON object in the judge's reply")


class JudgeRunner:
    """Interface: judge_image(png_path) -> the parsed verdict dict (left/right/preference). Raises JudgeError."""

    judge_id = "unset"
    model = "unset"
    family = "unknown"
    protected_paths = ()  # run_session sets this to the session dir; the scratch dir may not overlap it
    requires_sealed = True  # a runner that is a general agent may only run next to a SEALED session (look_seal.py)

    def identity(self):
        return {"judgeId": self.judge_id, "model": self.model, "family": self.family}

    def isolation_record(self):
        """What run_session writes into the results file and look_tally reads back: whether this runner's isolation is
        enforced by the harness, and a digest of the exact confinement it asks for."""
        return {"enforced": False, "kind": "unspecified"}

    def judge_image(self, png_path, rubric_text):  # pragma: no cover - interface
        raise NotImplementedError


class CallableJudge(JudgeRunner):
    """Wraps a function (tests, or a lane that judges out of process). The harness cannot enforce what such a judge can
    read, so it records `enforced: False` unless the caller states otherwise with `isolation` (a claim, not a proof)."""

    requires_sealed = False

    def __init__(self, fn, judge_id="callable", model="callable", family="test", isolation=None):
        self.fn, self.judge_id, self.model, self.family = fn, judge_id, model, family
        self._isolation = isolation

    def isolation_record(self):
        return dict(self._isolation) if self._isolation else {"enforced": False, "kind": "callable"}

    def judge_image(self, png_path, rubric_text):
        return self.fn(png_path, rubric_text)


def _canon(path):
    return os.path.normcase(os.path.realpath(path))


def assert_isolated(work, protected_paths):
    """Raise JudgeError when the judge's scratch dir is, contains, or sits inside any protected path (the session
    directory: answer_key.json, source-frames/, sibling items). A scratch dir that overlaps the session cannot be
    isolated from it, whatever flags the CLI is given."""
    here = _canon(work)
    for protected in protected_paths or ():
        other = _canon(protected)
        if here == other or here.startswith(other + os.sep) or other.startswith(here + os.sep):
            raise JudgeError(f"the judge's scratch dir {work!r} overlaps the protected path {protected!r}: "
                             "the judge could read what it must not see; set TMP/TEMP somewhere else")


def _neutral_workdir(png_path, protected_paths=()):
    """A fresh temp dir holding ONLY pair.png, so the judge cannot see sibling items or the answer key. Refuses
    (before anything is copied) when that dir would overlap a protected path."""
    work = tempfile.mkdtemp(prefix="lookjudge-")
    try:
        assert_isolated(work, protected_paths)
    except JudgeError:
        _remove_workdir(work)
        raise
    shutil.copyfile(png_path, os.path.join(work, "pair.png"))
    return work


def _remove_workdir(work):
    # Non-recursive on purpose: this directory only ever holds the files named here.
    for name in os.listdir(work):
        try:
            os.remove(os.path.join(work, name))
        except OSError:
            pass
    try:
        os.rmdir(work)
    except OSError:
        pass


CLAUDE_CONFINEMENT_ARGS = ("--restricted", "--safe-mode", "--strict-mcp-config")


def _template_digest(argv):
    return hashlib.sha256(json.dumps(list(argv)).encode("utf-8")).hexdigest()


class ClaudeCliJudge(JudgeRunner):
    def __init__(self, model, judge_id=None, claude_exe=None, timeout_s=300, extra_args=None):
        self.model = model
        self.family = family_of(model)
        self.judge_id = judge_id or f"claude-cli:{model}"
        self.claude_exe = claude_exe or shutil.which("claude") or "claude"
        self.timeout_s = timeout_s
        self.extra_args = list(extra_args or [])

    def command(self, confined=True):
        """ISOLATION: --restricted ignores the user's, the project's and the local settings and confines the file
        tools to the working directory (which holds only pair.png); --safe-mode switches off CLAUDE.md, hooks,
        skills, plugins and MCP servers; --strict-mcp-config with no --mcp-config means no MCP server at all; no
        --add-dir widens the roots. A CLI too old to know a flag exits non-zero (JudgeError), it does not run
        unconfined. `confined=False` exists ONLY for the isolation canary's control run (look_cli isolation-canary):
        judge_image never calls it."""
        return ([self.claude_exe, "-p", "--model", self.model, "--tools", "Read", "--allowedTools", "Read"]
                + (list(CLAUDE_CONFINEMENT_ARGS) if confined else [])
                + ["--no-session-persistence", "--disable-slash-commands", "--output-format", "json"]
                + self.extra_args)

    def isolation_record(self):
        return {"enforced": True, "kind": "claude-cli", "tools": ["Read"], "confinement": list(CLAUDE_CONFINEMENT_ARGS),
                "commandSha256": _template_digest(self.command()[1:]), "sealRequired": True}

    def judge_image(self, png_path, rubric_text):
        assert_no_seal_key_in_environment()
        work = _neutral_workdir(png_path, self.protected_paths)
        try:
            proc = run_bounded(self.command(), input_text=build_prompt(rubric_text), cwd=work,
                               timeout=self.timeout_s, env=judge_environment())
        except subprocess.TimeoutExpired as exc:
            raise JudgeError(f"claude timed out after {self.timeout_s}s (process tree killed)") from exc
        except OSError as exc:
            raise JudgeError(f"claude could not start: {exc}") from exc
        finally:
            _remove_workdir(work)
        if proc.returncode != 0:
            raise JudgeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}")
        try:
            outer = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"claude output is not JSON: {proc.stdout[:200]!r}") from exc
        if outer.get("is_error"):
            raise JudgeError(f"claude reported an error: {str(outer.get('result'))[:400]}")
        denials = outer.get("permission_denials")
        if not isinstance(denials, list):
            raise JudgeError("claude's reply carries no permission_denials list: cannot verify the judge stayed in its "
                             "directory, so the verdict is rejected")
        if denials:  # the judge TRIED to reach something it may not: the verdict is not trusted, and the attempt is recorded
            raise JudgeError(f"claude was denied {len(denials)} tool call(s) (a read outside its directory?): "
                             f"{json.dumps(denials)[:400]}; verdict rejected")
        verdict = extract_json_object(str(outer.get("result", "")))
        verdict["_raw"] = str(outer.get("result", ""))[:2000]
        return verdict


class CodexExecJudge(JudgeRunner):
    """`model=None` means 'the Codex default': it is resolved to the real model name from config.toml (so the
    entry records what actually judged, and the producer guard can compare it), and only when config.toml names
    none is it recorded as `codex-default`, which the guard treats as ANY OpenAI model."""

    def __init__(self, model=None, judge_id=None, codex_exe=None, timeout_s=300, codex_home=None):
        self.default_resolved = model is None and resolve_codex_default_model(codex_home) is not None
        self.model = model or resolve_codex_default_model(codex_home) or CODEX_DEFAULT_MODEL
        self.family = "openai"
        self.judge_id = judge_id or f"codex-exec:{self.model}"
        self.codex_exe = codex_exe or shutil.which("codex") or "codex"
        self.timeout_s = timeout_s

    def _isolation_args(self):
        args = ["--ignore-user-config", "--ignore-rules", "--strict-config"]
        for feature in CODEX_DISABLED_FEATURES:
            args += ["--disable", feature]
        return args + ["-c", "web_search=disabled"]

    def command(self, png_path, out_file, work, confined=True):
        # The prompt goes on stdin: --image is variadic and would swallow a trailing positional prompt.
        # ISOLATION: the read-only sandbox lets a shell read ANY file the user can (Codex maps it to read access at the
        # filesystem root, and view_image takes an absolute path), so no sandbox flag is relied on. The tools that could
        # read a file are switched off instead (CODEX_DISABLED_FEATURES): the judge can only look at the attached
        # picture. --strict-config / an unknown --disable make a CLI that no longer knows one of these fail loudly
        # rather than run with a tool on. --ignore-user-config / --ignore-rules keep the user's config.toml, rules and
        # MCP servers out; that is safe for the model choice because the model is passed with -m whenever it is known
        # (see __init__). `confined=False` exists ONLY for the isolation canary's control run; judge_image never uses it.
        cmd = [self.codex_exe, "exec", "--image", png_path, "--sandbox", "read-only", "--skip-git-repo-check"]
        if confined:
            cmd += self._isolation_args()
        cmd += ["--ephemeral", "-C", work, "-o", out_file]
        if self.model != CODEX_DEFAULT_MODEL:
            cmd[2:2] = ["-m", self.model]
        return cmd

    def isolation_record(self):
        return {"enforced": True, "kind": "codex-exec", "toolsDisabled": list(CODEX_DISABLED_FEATURES),
                "commandSha256": _template_digest(self.command("<png>", "<out>", "<work>")[1:]), "sealRequired": True}

    def judge_image(self, png_path, rubric_text):
        assert_no_seal_key_in_environment()
        work = _neutral_workdir(png_path, self.protected_paths)
        out_file = os.path.join(work, "last-message.txt")
        try:
            proc = run_bounded(
                self.command(os.path.join(work, "pair.png"), out_file, work),
                input_text=build_prompt(rubric_text, True), cwd=work, timeout=self.timeout_s,
                env=judge_environment())
            text = ""
            if os.path.isfile(out_file):
                with open(out_file, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
        except subprocess.TimeoutExpired as exc:
            raise JudgeError(f"codex timed out after {self.timeout_s}s (process tree killed)") from exc
        except OSError as exc:
            raise JudgeError(f"codex could not start: {exc}") from exc
        finally:
            _remove_workdir(work)
        if proc.returncode != 0:
            raise JudgeError(f"codex exited {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")
        verdict = extract_json_object(text or proc.stdout)
        verdict["_raw"] = (text or proc.stdout)[:2000]
        return verdict


# ---------------------------------------------------------------------------------------------------------
# cross-family probes
# ---------------------------------------------------------------------------------------------------------

def probe_codex_image_support(codex_exe=None, timeout_s=60):
    """Read-only: does `codex exec --help` list an image-input option? Never calls a model."""
    exe = codex_exe or shutil.which("codex")
    if not exe:
        return {"status": CROSS_FAMILY_UNAVAILABLE, "reason": "codex executable not found"}
    try:
        proc = run_bounded([exe, "--version"], timeout=timeout_s)
        version = (proc.stdout or proc.stderr).strip()
        proc = run_bounded([exe, "exec", "--help"], timeout=timeout_s)
        help_text = proc.stdout + proc.stderr
        features = run_bounded([exe, "features", "list"], timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": CROSS_FAMILY_UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}
    has_flag = "--image" in help_text
    known = {line.split()[0] for line in (features.stdout or "").splitlines() if line.split()}
    unknown = [f for f in CODEX_DISABLED_FEATURES if f not in known]
    # The judge's isolation IS its disabled tools: a Codex that no longer knows one of them cannot be confined, so it
    # does not judge (the runner would fail on the unknown --disable anyway; this records WHY, up front).
    isolatable = not unknown and "--strict-config" in help_text
    if has_flag and not isolatable:
        reason = (f"isolation cannot be enforced: this Codex does not list the features {unknown}" if unknown
                  else "isolation cannot be enforced: `codex exec --help` lists no --strict-config")
    else:
        reason = None if has_flag else "`codex exec --help` lists no image-input option"
    return {
        "status": CROSS_FAMILY_FLAG_ONLY if (has_flag and isolatable) else CROSS_FAMILY_UNAVAILABLE,
        "codexVersion": version, "imageFlag": "--image" if has_flag else None,
        "helpSha256": hashlib.sha256(help_text.encode("utf-8")).hexdigest(),
        "isolationFeaturesUnknown": unknown, "reason": reason,
    }


def probe_codex_vision(png_path, expected_word, codex_exe=None, model=None, timeout_s=240):
    """LIVE proof that a Codex lane sees pixels: attach a synthetic image whose answer is only in the pixels
    (e.g. a green square on a red field -> 'green') and require the word back. One bounded call."""
    judge = CodexExecJudge(model=model, codex_exe=codex_exe, timeout_s=timeout_s)
    work = _neutral_workdir(png_path)
    out_file = os.path.join(work, "last-message.txt")
    prompt = ("Look at the attached image. A single square sits on a plain background. Reply with ONLY the "
              "English name of the square's colour, in lower case, and nothing else.")
    try:
        proc = run_bounded(judge.command(os.path.join(work, "pair.png"), out_file, work), input_text=prompt,
                           cwd=work, timeout=timeout_s, env=judge_environment())
        text = ""
        if os.path.isfile(out_file):
            with open(out_file, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
    except subprocess.TimeoutExpired:
        return {"status": CROSS_FAMILY_FLAG_ONLY, "reason": f"vision probe timed out after {timeout_s}s"}
    except OSError as exc:
        return {"status": CROSS_FAMILY_FLAG_ONLY, "reason": f"vision probe could not start: {exc}"}
    finally:
        _remove_workdir(work)
    if proc.returncode != 0:
        return {"status": CROSS_FAMILY_FLAG_ONLY, "reason": f"codex exited {proc.returncode}",
                "stderrTail": (proc.stderr or "")[-300:]}
    answer = (text or proc.stdout).strip().lower()
    seen = expected_word.lower() in answer
    return {"status": CROSS_FAMILY_PROVEN if seen else CROSS_FAMILY_FLAG_ONLY, "expected": expected_word,
            "answer": answer[:200], "model": judge.model}


def select_second_judge(primary_family, probe, codex_model=None, fallback_claude_model=None):
    """Policy: a Codex judge only when the image probe is PROVEN; otherwise a second Claude model (a different
    one from the primary judge) and CROSS_FAMILY_UNAVAILABLE. Returns (runner_or_None, status)."""
    if probe.get("status") == CROSS_FAMILY_PROVEN and primary_family != "openai":
        return CodexExecJudge(model=codex_model), CROSS_FAMILY_PROVEN
    if fallback_claude_model:
        return ClaudeCliJudge(fallback_claude_model), CROSS_FAMILY_UNAVAILABLE
    return None, CROSS_FAMILY_UNAVAILABLE


# ---------------------------------------------------------------------------------------------------------
# session runner
# ---------------------------------------------------------------------------------------------------------

def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def results_path(session_dir, judge_id):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in judge_id)
    return os.path.join(session_dir, f"verdicts-{safe}.json")


def run_session(session_dir, runner, workers=3, deadline_s=500, max_items=None, rubric_path=None, lock_path=None):
    """Judge every item of a built session with `runner`. REFUSES unless the rubric still matches its lock AND
    the session recorded that same digest before judging AND the images on disk are the ones the session recorded.
    Resumable, but a stored verdict is only kept while it still describes the CURRENT image and the CURRENT
    rubric: anything else is thrown away (and listed under `staleRejected`) and the item is judged again."""
    lock = look_config.verify_rubric_lock(rubric_path, lock_path)
    session = _load_json(os.path.join(session_dir, "session.json"))
    manifest = _load_json(os.path.join(session_dir, "judge_manifest.json"))
    if not session or not manifest:
        raise JudgeError("session.json / judge_manifest.json missing: build the session first")
    if session.get("rubricSha256") != lock["rubricSha256"]:
        raise look_config.RubricLockError(
            "session recorded a different rubric digest than the lock: judging refused (rubric changed after freeze)")
    current = {}
    for entry in manifest["items"]:
        try:
            current[entry["itemId"]] = sha256_file(os.path.join(session_dir, entry["image"]))
        except OSError as exc:
            raise JudgeError(f"pair image of item {entry['itemId']} is unreadable: {exc}") from exc
    if sorted(current.values()) != sorted(session.get("imageSha256s") or []):
        raise JudgeError("the pair images on disk do not match the digests the session recorded: "
                         "the session was changed after it was built; rebuild it")
    rubric_text = look_config.load_rubric_text(rubric_path)
    state = look_seal.session_state(session_dir)
    if runner.requires_sealed:
        assert_no_seal_key_in_environment()
        try:
            look_seal.assert_judgeable(session_dir)
        except look_seal.SealError as exc:
            raise JudgeError(str(exc)) from exc
    judged_under_seal = state["sealed"] and not state["plaintextPresent"]
    seal_sha = state["sealSha256"] if judged_under_seal else None
    isolation = runner.isolation_record()
    runner.protected_paths = [os.path.abspath(session_dir)]  # the judge's scratch dir may never overlap the session
    out_path = results_path(session_dir, runner.judge_id)
    existing = _load_json(out_path)
    if existing is not None and existing.get("judge") != runner.identity():
        raise JudgeError(f"{out_path} belongs to judge {existing.get('judge')!r}, not {runner.identity()!r}: "
                         "refusing to mix verdicts from different judge identities")
    doc = existing or {
        "schema": "mlv-app/look-judge-results/v1", "judge": runner.identity(),
        "rubricSha256": lock["rubricSha256"], "items": {}, "errors": {},
    }
    stale = {}
    # Verdicts made before the session was sealed, or under another confinement, were made by a judge that could have
    # read the key: they are thrown away and judged again, never carried into a sealed run.
    changed_conditions = existing is not None and (existing.get("sealSha256") != seal_sha
                                                   or existing.get("isolation") != isolation)
    for item_id, stored in list(doc["items"].items()):
        if changed_conditions:
            stale[item_id] = "SEAL_OR_ISOLATION_DIFFERS_FROM_CURRENT_RUN"
        elif item_id not in current:
            stale[item_id] = "ITEM_NOT_IN_THIS_SESSION"
        elif stored.get("imageSha256") != current[item_id]:
            stale[item_id] = "IMAGE_DIGEST_DIFFERS_FROM_CURRENT_IMAGE"
        elif stored.get("rubricSha256") != lock["rubricSha256"]:
            stale[item_id] = "RUBRIC_DIGEST_DIFFERS_FROM_LOCK"
    for item_id in stale:
        del doc["items"][item_id]
    doc["rubricSha256"] = lock["rubricSha256"]
    doc["sealSha256"] = seal_sha
    doc["judgedUnderSeal"] = judged_under_seal
    doc["isolation"] = isolation
    doc["staleRejected"] ={**doc.get("staleRejected", {}), **stale}
    todo = [it for it in manifest["items"] if it["itemId"] not in doc["items"]]
    if max_items is not None:
        todo = todo[:max_items]
    guard = threading.Lock()
    started = time.monotonic()

    def one(entry):
        png = os.path.join(session_dir, entry["image"])
        digest = sha256_file(png)
        if digest != current[entry["itemId"]]:
            raise JudgeError("the pair image changed after the session check")
        verdict = dict(runner.judge_image(png, rubric_text))
        if sha256_file(png) != digest:
            raise JudgeError("the pair image changed while it was being judged")
        verdict["rubricSha256"] = lock["rubricSha256"]
        verdict["imageSha256"] = digest
        return verdict

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i in range(0, len(todo), max(1, workers)):
            if time.monotonic() - started > deadline_s:
                break
            chunk = todo[i:i + max(1, workers)]
            futures = {pool.submit(one, e): e for e in chunk}
            for fut in concurrent.futures.as_completed(futures):
                entry = futures[fut]
                with guard:
                    try:
                        doc["items"][entry["itemId"]] = fut.result()
                        doc["errors"].pop(entry["itemId"], None)
                    except Exception as exc:  # a failed item is recorded, never scored
                        doc["errors"][entry["itemId"]] = f"{type(exc).__name__}: {exc}"
                    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
                        json.dump(doc, handle, indent=2)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:  # also records a run that judged nothing new
        json.dump(doc, handle, indent=2)
    remaining = [it["itemId"] for it in manifest["items"] if it["itemId"] not in doc["items"]]
    return {"resultsPath": out_path, "judged": len(doc["items"]), "errors": len(doc["errors"]),
            "remaining": len(remaining), "staleRejected": len(stale), "judge": runner.identity()}
