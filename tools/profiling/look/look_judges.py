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

JUDGES ARE NEVER THE PRODUCER OR THE HUB: assert_not_producer() refuses a judge whose model matches a
producer/hub model id the caller names.

BLINDING: each item runs in its own temp directory containing only `pair.png`; the prompt is the frozen rubric
plus a fixed instruction. Nothing about subjects, backends, flavors or seeds is ever in a prompt or a path.
"""
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import look_config

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


def family_of(model):
    m = (model or "").lower()
    if m.startswith(("claude", "opus", "sonnet", "haiku", "fable")):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai"
    return "unknown"


def assert_not_producer(judge_model, forbidden_models):
    """Raise ProducerJudgeError when the judge's model is one the caller lists as the producer or the hub."""
    jm = (judge_model or "").strip().lower()
    if not jm:
        raise ProducerJudgeError("a judge must name its model")
    for bad in forbidden_models or ():
        b = (bad or "").strip().lower()
        if b and (jm == b or jm.startswith(b) or b.startswith(jm)):
            raise ProducerJudgeError(f"judge model {judge_model!r} is the producer/hub model {bad!r}")


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

    def identity(self):
        return {"judgeId": self.judge_id, "model": self.model, "family": self.family}

    def judge_image(self, png_path, rubric_text):  # pragma: no cover - interface
        raise NotImplementedError


class CallableJudge(JudgeRunner):
    def __init__(self, fn, judge_id="callable", model="callable", family="test"):
        self.fn, self.judge_id, self.model, self.family = fn, judge_id, model, family

    def judge_image(self, png_path, rubric_text):
        return self.fn(png_path, rubric_text)


def _neutral_workdir(png_path):
    """A fresh temp dir holding ONLY pair.png, so the judge cannot see sibling items or the answer key."""
    work = tempfile.mkdtemp(prefix="lookjudge-")
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


class ClaudeCliJudge(JudgeRunner):
    def __init__(self, model, judge_id=None, claude_exe=None, timeout_s=300, extra_args=None):
        self.model = model
        self.family = family_of(model)
        self.judge_id = judge_id or f"claude-cli:{model}"
        self.claude_exe = claude_exe or shutil.which("claude") or "claude"
        self.timeout_s = timeout_s
        self.extra_args = list(extra_args or [])

    def command(self):
        return [self.claude_exe, "-p", "--model", self.model, "--tools", "Read", "--allowedTools", "Read",
                "--no-session-persistence", "--disable-slash-commands", "--output-format", "json"] + self.extra_args

    def judge_image(self, png_path, rubric_text):
        work = _neutral_workdir(png_path)
        try:
            proc = subprocess.run(
                self.command(), input=build_prompt(rubric_text), cwd=work, capture_output=True, text=True,
                encoding="utf-8", timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise JudgeError(f"claude timed out after {self.timeout_s}s") from exc
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
        verdict = extract_json_object(str(outer.get("result", "")))
        verdict["_raw"] = str(outer.get("result", ""))[:2000]
        return verdict


class CodexExecJudge(JudgeRunner):
    def __init__(self, model=None, judge_id=None, codex_exe=None, timeout_s=300):
        self.model = model or "codex-default"
        self.family = "openai"
        self.judge_id = judge_id or f"codex-exec:{self.model}"
        self.codex_exe = codex_exe or shutil.which("codex") or "codex"
        self.timeout_s = timeout_s

    def command(self, png_path, out_file, work):
        # The prompt goes on stdin: --image is variadic and would swallow a trailing positional prompt.
        cmd = [self.codex_exe, "exec", "--image", png_path, "--sandbox", "read-only", "--skip-git-repo-check",
               "--ephemeral", "-C", work, "-o", out_file]
        if self.model != "codex-default":
            cmd[2:2] = ["-m", self.model]
        return cmd

    def judge_image(self, png_path, rubric_text):
        work = _neutral_workdir(png_path)
        out_file = os.path.join(work, "last-message.txt")
        try:
            proc = subprocess.run(
                self.command(os.path.join(work, "pair.png"), out_file, work), input=build_prompt(rubric_text, True),
                cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout_s,
            )
            text = ""
            if os.path.isfile(out_file):
                with open(out_file, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
        except subprocess.TimeoutExpired as exc:
            raise JudgeError(f"codex timed out after {self.timeout_s}s") from exc
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
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=timeout_s)
        version = (proc.stdout or proc.stderr).strip()
        proc = subprocess.run([exe, "exec", "--help"], capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": CROSS_FAMILY_UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}"}
    help_text = proc.stdout + proc.stderr
    has_flag = "--image" in help_text
    return {
        "status": CROSS_FAMILY_FLAG_ONLY if has_flag else CROSS_FAMILY_UNAVAILABLE,
        "codexVersion": version, "imageFlag": "--image" if has_flag else None,
        "helpSha256": hashlib.sha256(help_text.encode("utf-8")).hexdigest(),
        "reason": None if has_flag else "`codex exec --help` lists no image-input option",
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
        proc = subprocess.run(
            judge.command(os.path.join(work, "pair.png"), out_file, work), input=prompt, cwd=work,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
        )
        text = ""
        if os.path.isfile(out_file):
            with open(out_file, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
    except subprocess.TimeoutExpired:
        return {"status": CROSS_FAMILY_FLAG_ONLY, "reason": f"vision probe timed out after {timeout_s}s"}
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


def results_path(session_dir, judge_id):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in judge_id)
    return os.path.join(session_dir, f"verdicts-{safe}.json")


def run_session(session_dir, runner, workers=3, deadline_s=500, max_items=None, rubric_path=None, lock_path=None):
    """Judge every item of a built session with `runner`. REFUSES unless the rubric still matches its lock AND
    the session recorded that same digest before judging. Resumable: items already answered are skipped."""
    lock = look_config.verify_rubric_lock(rubric_path, lock_path)
    session = _load_json(os.path.join(session_dir, "session.json"))
    manifest = _load_json(os.path.join(session_dir, "judge_manifest.json"))
    if not session or not manifest:
        raise JudgeError("session.json / judge_manifest.json missing: build the session first")
    if session.get("rubricSha256") != lock["rubricSha256"]:
        raise look_config.RubricLockError(
            "session recorded a different rubric digest than the lock: judging refused (rubric changed after freeze)")
    rubric_text = look_config.load_rubric_text(rubric_path)
    out_path = results_path(session_dir, runner.judge_id)
    doc = _load_json(out_path) or {
        "schema": "mlv-app/look-judge-results/v1", "judge": runner.identity(),
        "rubricSha256": lock["rubricSha256"], "items": {}, "errors": {},
    }
    todo = [it for it in manifest["items"] if it["itemId"] not in doc["items"]]
    if max_items is not None:
        todo = todo[:max_items]
    guard = threading.Lock()
    started = time.monotonic()

    def one(entry):
        png = os.path.join(session_dir, entry["image"])
        verdict = runner.judge_image(png, rubric_text)
        verdict = dict(verdict)
        verdict["rubricSha256"] = lock["rubricSha256"]
        with open(png, "rb") as image_handle:
            verdict["imageSha256"] = hashlib.sha256(image_handle.read()).hexdigest()
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
    remaining = [it["itemId"] for it in manifest["items"] if it["itemId"] not in doc["items"]]
    return {"resultsPath": out_path, "judged": len(doc["items"]), "errors": len(doc["errors"]),
            "remaining": len(remaining), "judge": runner.identity()}
