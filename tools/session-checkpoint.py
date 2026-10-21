#!/usr/bin/env python3
"""session-checkpoint v3. SessionStart: detect account rotation, list dirty
or ahead worktrees, print newest checkpoints, re-seed runbook, snapshot dirt,
and retain resumption pointers for explicitly session-owned lane work.
Stop: write pointer-only checkpoint for THIS session to
Home/.claude/session-checkpoints/<repo-slug>/ with lane resumption metadata.
Never runs a git write or generates commands that mutate a checkout or relaunch
a provider. Receipts without an explicit full sessionId cannot establish lane
ownership and are omitted. Process completion is not delivery acceptance.
Owned dirt = files dirty now that were not dirty at SessionStart."""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

RUNBOOK_CANDIDATES = ["docs/ops/ROTATION.md", "knowledge/ROTATION.md",
                      "roadmap/handoff/ROTATION.md", "docs/ROTATION.md"]


def git(cwd, *args):
    try:
        p = subprocess.run(["git", "-C", str(cwd), "--no-optional-locks", *args],
                           capture_output=True, text=True, timeout=60)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def account_id():
    try:
        d = json.load(open(Path.home() / ".claude.json", encoding="utf-8"))
        return str((d.get("oauthAccount") or {}).get("accountUuid") or "")[:8]
    except Exception:
        return ""


def worktree_scan(repo):
    out = []
    paths = [l[9:] for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
    if not paths:
        return out
    main = git(paths[0], "symbolic-ref", "-q", "--short", "HEAD") or "HEAD"
    for p in paths:
        dirty = len(git(p, "diff", "--name-only").splitlines()) + len(git(p, "ls-files", "--others", "--exclude-standard").splitlines())
        ahead = git(p, "rev-list", "--count", f"{main}..HEAD") if p != paths[0] else "0"
        if dirty or (ahead not in ("", "0")):
            out.append(f"  {p}: dirty={dirty} ahead-of-{main}={ahead or '?'}")
    return out


def scan_fleet_runs(repo, session_id):
    """Read pointers only from receipts naming this exact, nonempty session."""
    fleet_runs = repo / ".claude-state" / "fleet-runs"
    if not session_id or not fleet_runs.exists():
        return None

    # Receipts sit at depth 0 or 1 only. 2026-10-20: rglob walked the ~95 full
    # repo copies under hub-tools and pinned the host (93% CPU, ~100k file ops/s).
    receipts = [*fleet_runs.glob("*-*.receipt.json"), *fleet_runs.glob("*/*-*.receipt.json")]

    # Look for receipts from this session
    for receipt_file in sorted(receipts, reverse=True):
        try:
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("sessionId") != session_id:
                continue

            lane = receipt.get("lane", "unknown")
            card = receipt.get("card", "")
            explicit_prompt = receipt.get("promptPath")
            if explicit_prompt is not None and not isinstance(explicit_prompt, str):
                continue
            prompt_path = (Path(explicit_prompt) if explicit_prompt else
                           receipt_file.with_name(receipt_file.name.removesuffix(".receipt.json") + ".prompt.txt"))

            # Process lifecycle only; exit zero does not accept the delivery.
            exit_code = receipt.get("exitCode")
            if exit_code is None:
                status = "IN_PROGRESS"
            elif exit_code == 0:
                status = "PROCESS_EXITED"
            else:
                status = f"FAILED (exit {exit_code})"

            # Preserve metadata; these pointers do not authorize resuming a provider.
            invocation = {
                "lane": lane,
                "card": card,
                "prompt_path": str(prompt_path),
                "status": status,
                "receipt": str(receipt_file),
                "output_path": receipt.get("outputPath"),
                "worktree": receipt.get("workDir"),
                "state": receipt.get("state"),
                "complete": receipt.get("complete"),
                "exit_code": exit_code,
            }
            return invocation
        except Exception:
            continue

    return None


def generate_resume_script(repo, branch, session_id, dirty_files, invocation):
    """Retain the historical helper name, but return descriptive pointers only."""
    if not invocation:
        return []
    lines = [f"- repository: {repo}", f"- branch: {branch}", f"- session: {session_id}"]
    for key in ("worktree", "lane", "card", "receipt", "prompt_path", "output_path", "status", "state"):
        value = invocation.get(key)
        if value is not None:
            lines.append(f"- {key}: {value}")
    lines.extend(f"- pending file: {path}" for path in dirty_files)
    lines.append("- Next action: inspect the receipt and worktree ownership before deciding how to continue.")
    return lines


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    cwd = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    session_id = str(payload.get("session_id") or "")
    sid = session_id[:8] or "unknown"
    event = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return 0

    repo = Path(common).parent
    home = Path.home() / ".claude"
    base = home / "session-checkpoints" / repo.name.replace(" ", "-")
    base.mkdir(parents=True, exist_ok=True)
    snap = base / f".snap-{sid}.json"
    acct = account_id()
    dirty = set(git(cwd, "diff", "--name-only").splitlines()) | \
        set(git(cwd, "ls-files", "--others", "--exclude-standard").splitlines())

    if event == "SessionStart":
        snap.write_text(json.dumps(sorted(dirty)), encoding="utf-8")
        seen = base / ".account"
        prev = seen.read_text(encoding="utf-8").strip() if seen.exists() else ""
        if acct:
            seen.write_text(acct, encoding="utf-8")
        if not (home / "ROTATION.md").exists():
            for c in RUNBOOK_CANDIDATES:
                if (repo / c).exists():
                    shutil.copyfile(repo / c, home / "ROTATION.md")
                    break

        if prev and acct and prev != acct:
            print(f"ACCOUNT ROTATION DETECTED ({prev} -> {acct}): read {home / 'ROTATION.md'} section 4 before deriving anything.")

        scan = worktree_scan(repo)
        if scan:
            print(f"Worktrees of {repo.name} with uncommitted or unintegrated work:")
            print("\n".join(scan))

        files = sorted(base.glob("SESSION-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        if files:
            print(f"Session checkpoints for {repo.name} (newest first, pointer-only, machine-local):")
            for f in files:
                print(f"  {f}")

        return 0

    if event != "Stop":
        return 0

    # Stop hook: write checkpoint with lane resumption metadata (Phase 1)
    before = set(json.loads(snap.read_text(encoding="utf-8"))) if snap.exists() else set()
    owned = sorted(dirty - before)
    branch = git(cwd, "symbolic-ref", "-q", "--short", "HEAD") or "(detached)"
    head = git(cwd, "rev-parse", "--short", "HEAD")
    subject = git(cwd, "log", "-1", "--format=%s")[:120]
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # PHASE 1: Scan for in-flight lane work
    invocation = scan_fleet_runs(repo, session_id)

    # PHASE 2+3: Retain resumption pointers without executable instructions.
    resume_script = generate_resume_script(repo, branch, session_id, owned, invocation)

    # Write checkpoint
    out = base / f"SESSION-{sid}.md"
    lines = [f"# SESSION-{sid} checkpoint (pointer-only, written mechanically at every turn end)", "",
             f"- written: {ts}", f"- account: {acct or 'unknown'}", f"- repo: {repo}", f"- cwd: {cwd}",
             f"- branch: {branch} @ {head}", f"- last commit: {subject}",
             f"- files this session dirtied and has NOT committed ({len(owned)}):"]
    lines += [f"  - {p}" for p in owned] or ["  - none"]

    if invocation:
        lines += ["", "## LANE RESUMPTION (PHASE 1-3)", "",
                  f"- lane: {invocation.get('lane')}",
                  f"- status: {invocation.get('status')}",
                  f"- prompt: {invocation.get('prompt_path')}"]
        if invocation.get('card'):
            lines += [f"- card: {invocation.get('card')}"]

    if resume_script:
        lines += ["", "## RESUMPTION POINTERS (PHASE 2-3)", ""]
        lines += resume_script

    lines += ["", "Resume: read the repo entry file, then `git log -5 <branch>`; the list above is what a rotation would lose."]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if owned or invocation:
        if owned:
            print(f"checkpoint: {len(owned)} uncommitted file(s) from this session recorded at {out}")
        if invocation:
            print(f"checkpoint: lane {invocation.get('lane')} resumption pointers recorded")

    return 0


if __name__ == "__main__":
    sys.exit(main())
