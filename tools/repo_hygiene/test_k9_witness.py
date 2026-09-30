"""Tests for the K9 pushed-tree resumability witness (tools/repo_hygiene/k9_witness.py).

Every fixture lives in a temp directory with an isolated git environment
(empty global config, no system config, HOME redirected). No test touches a
real repository or any real .git/hooks directory. A bare remote plus a work
clone stand in for the landing seam; the hook is installed into the temp work
clone only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene import k9_witness  # noqa: F401  (import proves the module exists)

ROOT = Path(__file__).resolve().parents[2]
WITNESS = ROOT / "tools" / "repo_hygiene" / "k9_witness.py"
PINNED_REF = "refs/remotes/fork/master"
VERDICT_RE = re.compile(
    r"^k9-witness\.v1 verdict=(PASS|RED|UNKNOWN) tree=([0-9a-f]{40}|none) checks=(\d+) failed=(\S+)$"
)
GIT_IDENT = [
    "-c", "user.name=k9-test", "-c", "user.email=k9-test@example.invalid",
    "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
    "-c", "protocol.file.allow=always",
]

CLAUDE_MD = "# Fixture\n\nSee [agents/factory-kernel-instance.md](agents/factory-kernel-instance.md).\n"
ROTATION_MD = "# Rotation\n\nRotation is an owner act.\n"
CHECKPOINT_PY = "print('checkpoint')\n"
GREEN_MECH = "`tools/session-checkpoint.py`, `.claude-state/RESUME.md`"


def instance_map(mech: str = GREEN_MECH, obs: str = "heartbeat status", gap: str = "board-local") -> str:
    return (
        "# Instance map\n\n"
        "| Clause | Mechanism | Observable | Gap |\n"
        "|---|---|---|---|\n"
        "| K1 roles | `agents/other.md` | receipt | none |\n"
        f"| K9 resume | {mech} | {obs} | {gap} |\n"
    )


def _rmtree(path: Path) -> None:
    def fix(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onerror=fix)


def parse_verdict(stdout: str) -> tuple[str, str, int, str]:
    last = stdout.strip().splitlines()[-1]
    m = VERDICT_RE.match(last)
    assert m, f"last stdout line is not a k9-witness.v1 verdict line: {last!r}"
    return m.group(1), m.group(2), int(m.group(3)), m.group(4)


class Fixture:
    """A bare remote, a work clone with a GREEN spine and the hook installed."""

    template: "Fixture | None" = None

    def __init__(self, root: Path):
        self.root = root
        cfg = root / "empty.gitconfig"
        if not cfg.exists():
            cfg.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1",
            HOME=str(root), USERPROFILE=str(root),
        )
        self.env = env
        self.remote = root / "remote.git"
        self.work = root / "work"

    # -- plumbing ----------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None, check: bool = True,
            extra_env: dict | None = None, input: bytes | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(
            ["git", *GIT_IDENT, "-C", str(cwd or self.work), *args],
            env=env, capture_output=True, input=input, timeout=120,
        )
        if check and p.returncode != 0:
            raise AssertionError(f"git {args} failed rc={p.returncode}: {p.stderr.decode(errors='replace')}")
        return p

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return self.git(*args, cwd=cwd).stdout.decode().strip()

    def k9(self, *args: str, input: str | None = None, cwd: Path | None = None,
           extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(
            [sys.executable, str(WITNESS), *args],
            cwd=str(cwd or self.work), env=env, capture_output=True, timeout=120,
            input=None if input is None else input.encode(),
        )
        return p

    def write(self, rel: str, text: str) -> None:
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())

    def commit(self, files: dict[str, str], msg: str = "c") -> str:
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "--", *files)
        self.git("commit", "-q", "-m", msg)
        return self.out("rev-parse", "HEAD")

    def remote_ref(self, ref: str) -> str | None:
        p = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "--quiet", ref],
            env=self.env, capture_output=True,
        )
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def push(self, *refspecs: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {"MLV_K9_WITNESS_REF": PINNED_REF}
        if extra_env:
            env.update(extra_env)
        return self.git("push", "origin", *refspecs, check=False, extra_env=env)

    def hook_path(self) -> Path:
        return Path(self.out("rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"))

    def remove_loose(self, sha: str) -> None:
        p = self.work / ".git" / "objects" / sha[:2] / sha[2:]
        assert p.exists(), f"object {sha} is not loose"
        os.chmod(p, stat.S_IWRITE)
        p.unlink()

    # -- construction ------------------------------------------------------
    @classmethod
    def build_template(cls, root: Path) -> "Fixture":
        fx = cls(root)
        fx.git("init", "-q", "--bare", "-b", "master", str(fx.remote), cwd=root)
        fx.git("init", "-q", "-b", "master", str(fx.work), cwd=root)
        fx.git("remote", "add", "origin", str(fx.remote))
        fx.commit({"README.md": "fixture\n"}, "B0 no spine")
        fx.commit({
            "CLAUDE.md": CLAUDE_MD,
            "agents/factory-kernel-instance.md": instance_map(),
            "docs/ROTATION.md": ROTATION_MD,
            "tools/session-checkpoint.py": CHECKPOINT_PY,
            "tools/repo_hygiene/k9_witness.py": WITNESS.read_text(encoding="utf-8"),
        }, "G1 green spine")
        fx.git("push", "-q", "--no-verify", "origin", "master")
        fx.git("update-ref", PINNED_REF, "HEAD")
        p = fx.k9("install", "--repo", str(fx.work), "--ref", PINNED_REF)
        assert p.returncode == 0, p.stdout.decode() + p.stderr.decode()
        return fx

    def clone_to(self, root: Path) -> "Fixture":
        shutil.copytree(self.root, root, dirs_exist_ok=True)
        fx = Fixture(root)
        fx.git("remote", "set-url", "origin", str(fx.remote))
        return fx


_TEMPLATE_DIR: Path | None = None
_TEMPLATE: Fixture | None = None


def setUpModule():  # noqa: N802
    global _TEMPLATE_DIR, _TEMPLATE
    _TEMPLATE_DIR = Path(tempfile.mkdtemp(prefix="k9tmpl-"))
    _TEMPLATE = Fixture.build_template(_TEMPLATE_DIR)


def tearDownModule():  # noqa: N802
    if _TEMPLATE_DIR is not None:
        _rmtree(_TEMPLATE_DIR)


class K9Base(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="k9t-"))
        self.addCleanup(_rmtree, self._tmp)
        assert _TEMPLATE is not None
        self.fx = _TEMPLATE.clone_to(self._tmp)
        self.base = self.fx.out("rev-parse", "HEAD")  # G1


class TreeModeTests(K9Base):
    def test_green_tip_passes_and_reports_tree(self):
        p = self.fx.k9("tree", "HEAD")
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        verdict, tree, checks, failed = parse_verdict(p.stdout.decode())
        self.assertEqual((verdict, checks, failed), ("PASS", 4, "none"))
        self.assertEqual(tree, self.fx.out("rev-parse", "HEAD^{tree}"))

    def test_board_local_pointers_are_listed_but_exempt(self):
        p = self.fx.k9("tree", "HEAD")
        self.assertIn(".claude-state/RESUME.md", p.stdout.decode())
        self.assertIn("BOARD-LOCAL", p.stdout.decode())

    def test_json_mode_emits_parseable_object_then_verdict_line(self):
        p = self.fx.k9("tree", "HEAD", "--json")
        self.assertEqual(p.returncode, 0)
        lines = p.stdout.decode().strip().splitlines()
        doc = json.loads(lines[-2])
        self.assertEqual(doc["verdict"], "PASS")
        parse_verdict(p.stdout.decode())

    def test_missing_spine_is_red_c1(self):
        b0 = self.fx.out("rev-parse", "HEAD~1")
        p = self.fx.k9("tree", b0)
        self.assertEqual(p.returncode, 1)
        verdict, _, _, failed = parse_verdict(p.stdout.decode())
        self.assertEqual(verdict, "RED")
        self.assertIn("C1", failed.split(","))

    def test_claude_md_without_link_is_red_c2(self):
        self.fx.commit({"CLAUDE.md": "# no link here\n"})
        p = self.fx.k9("tree", "HEAD")
        self.assertEqual(p.returncode, 1)
        self.assertIn("C2", parse_verdict(p.stdout.decode())[3].split(","))

    def test_row_shape_violations_are_red_c2(self):
        good_row = f"| K9 resume | {GREEN_MECH} | obs | gap |\n"
        variants = {
            "three cells": "| K9 resume | `tools/session-checkpoint.py` | obs |\n",
            "empty cell": f"| K9 resume | {GREEN_MECH} |  | gap |\n",
            "five cells": f"| K9 resume | {GREEN_MECH} | obs | gap | extra |\n",
            "two rows": good_row + good_row,
        }
        for name, row in variants.items():
            with self.subTest(name):
                text = (
                    "# map\n\n| Clause | M | O | G |\n|---|---|---|---|\n" + row
                )
                sha = self.fx.commit({"agents/factory-kernel-instance.md": text})
                p = self.fx.k9("tree", sha)
                self.assertEqual(p.returncode, 1, p.stdout.decode())
                self.assertIn("C2", parse_verdict(p.stdout.decode())[3].split(","))

    def test_c3_missing_tracked_path_is_red(self):
        sha = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 1)
        self.assertIn("C3", parse_verdict(p.stdout.decode())[3].split(","))

    def test_c3_wrong_case_pointer_is_red(self):
        sha = self.fx.commit({
            "agents/factory-kernel-instance.md": instance_map(mech="`Tools/Session-Checkpoint.py`"),
        })
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 1)
        self.assertIn("C3", parse_verdict(p.stdout.decode())[3].split(","))

    def test_c3_only_board_local_tokens_is_red(self):
        sha = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`.claude-state/RESUME.md`")})
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 1)
        self.assertIn("C3", parse_verdict(p.stdout.decode())[3].split(","))

    # -- r2: sol r1 blockers 1 and 2 (C3 pointer readability, whitespace-bearing pointers) ------
    def _c3(self, mech: str, files: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        payload = {"agents/factory-kernel-instance.md": instance_map(mech=mech)}
        payload.update(files or {})
        sha = self.fx.commit(payload)
        return self.fx.k9("tree", sha)

    def test_c3_unreadable_pointer_blob_exits_2_never_pass(self):
        # sol r1 blocker 1: the pointer resolves in the tree listing, but its blob cannot be read.
        sha = self.fx.commit({"tools/session-checkpoint.py": "print('unique pointer blob r2')\n"})
        self.fx.remove_loose(self.fx.out("rev-parse", "HEAD:tools/session-checkpoint.py"))
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[:2], ("UNKNOWN", "none"))

    def test_c3_unreadable_second_pointer_blob_exits_2(self):
        # every resolved pointer is read, not only the first one
        p0 = self._c3(f"{GREEN_MECH}, `tools/extra-pointer.py`", {"tools/extra-pointer.py": "print('extra r2')\n"})
        self.assertEqual(p0.returncode, 0, p0.stdout.decode())
        self.fx.remove_loose(self.fx.out("rev-parse", "HEAD:tools/extra-pointer.py"))
        p = self.fx.k9("tree", "HEAD")
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())

    def test_c3_unreadable_pointer_blob_exits_2_in_pre_push_mode(self):
        sha = self.fx.commit({"tools/session-checkpoint.py": "print('unique pre-push blob r2')\n"})
        self.fx.remove_loose(self.fx.out("rev-parse", "HEAD:tools/session-checkpoint.py"))
        line = f"refs/heads/master {sha} refs/heads/master {self.base}\n"
        p = self.fx.k9("pre-push", "origin", "url", input=line)
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())

    def test_c3_whitespace_pointer_that_does_not_exist_is_red(self):
        # sol r1 blocker 2: `tools/nonexistent path.py` beside valid pointers used to PASS
        p = self._c3(f"{GREEN_MECH}, `tools/nonexistent path.py`")
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        verdict, _, _, failed = parse_verdict(p.stdout.decode())
        self.assertEqual((verdict, failed), ("RED", "C3"))
        self.assertIn("tools/nonexistent path.py", p.stdout.decode())

    def test_c3_whitespace_variants_are_all_checked(self):
        variants = {
            "tab": "`tools/non\texistent.py`",
            "two spaces": "`tools/non  existent.py`",
            "leading space": "` tools/nonexistent.py`",
            "trailing space": "`tools/nonexistent.py `",
            "both ends": "`  tools/nonexistent.py  `",
            "space in a directory": "`tools/no dir/nonexistent.py`",
        }
        for name, span in variants.items():
            with self.subTest(name):
                p = self._c3(f"{GREEN_MECH}, {span}")
                self.assertEqual(p.returncode, 1, p.stdout.decode())
                self.assertEqual(parse_verdict(p.stdout.decode())[3], "C3")

    def test_c3_tracked_pointer_with_spaces_resolves(self):
        p = self._c3(
            f"{GREEN_MECH}, `tools/my tool.py`, ` tools/other tool.py `",
            {"tools/my tool.py": "print('a')\n", "tools/other tool.py": "print('b')\n"},
        )
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        self.assertIn("3 tracked pointer(s) resolve", p.stdout.decode())

    def test_c3_only_a_whitespace_pointer_resolves_is_not_vacuous(self):
        p = self._c3("`tools/only tool.py`", {"tools/only tool.py": "print('c')\n"})
        self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_c3_whitespace_pointer_wrong_case_is_red(self):
        p = self._c3(f"{GREEN_MECH}, `Tools/My Tool.py`", {"tools/my tool.py": "print('a')\n"})
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        self.assertIn("tools/my tool.py", p.stdout.decode())

    def test_c3_unreadable_whitespace_pointer_blob_exits_2(self):
        self._c3(f"{GREEN_MECH}, `tools/my tool.py`", {"tools/my tool.py": "print('unique spaced r2')\n"})
        self.fx.remove_loose(self.fx.out("rev-parse", "HEAD:tools/my tool.py"))
        p = self.fx.k9("tree", "HEAD")
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())

    def test_c3_pointer_to_a_directory_is_red_not_a_blob(self):
        # a pointer whose path names a tree object (a directory with a dotted name), not a blob
        p = self._c3(f"{GREEN_MECH}, `tools/pkg.d`", {"tools/pkg.d/inner.py": "print('d')\n"})
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[3], "C3")
        p = self._c3(f"{GREEN_MECH}, `tools/pkg dir.d`", {"tools/pkg dir.d/inner.py": "print('d')\n"})
        self.assertEqual(p.returncode, 1, p.stdout.decode())

    def test_c3_anchored_and_line_suffixed_pointers_are_judged(self):
        # fable H5: `path#anchor` and `path:12` were skipped, not judged
        for span in ("`tools/missing.py#L1`", "`tools/missing.py:12`", "`tools/missing.py:12-20`",
                     "`tools/missing path.py#top`"):
            with self.subTest(span):
                p = self._c3(f"{GREEN_MECH}, {span}")
                self.assertEqual(p.returncode, 1, p.stdout.decode())
                self.assertEqual(parse_verdict(p.stdout.decode())[3], "C3")
        p = self._c3(f"{GREEN_MECH}, `tools/session-checkpoint.py#L1`, `tools/session-checkpoint.py:3`")
        self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_c3_command_span_without_a_path_shape_is_not_a_pointer(self):
        p = self._c3(f"{GREEN_MECH}, `python -m tools.repo_hygiene.k9_witness tree HEAD`, `-Status`, `a b`")
        self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_t5_full_sha_then_guid_in_k9_row_are_red_c4(self):
        derived = {
            "full sha": "0123456789abcdef0123456789abcdef01234567",
            "guid": "123e4567-e89b-12d3-a456-426614174000",
            "short sha": "1a2b3c4d5e",
            "pid": "worker pid=41234",
        }
        for name, value in derived.items():
            with self.subTest(name):
                sha = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(gap=f"see {value}")})
                p = self.fx.k9("tree", sha)
                self.assertEqual(p.returncode, 1, p.stdout.decode())
                verdict, _, _, failed = parse_verdict(p.stdout.decode())
                self.assertEqual((verdict, failed), ("RED", "C4"))

    def test_c4_applies_to_rotation_md(self):
        sha = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "last run 0123456789abcdef0123456789abcdef01234567\n"})
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(parse_verdict(p.stdout.decode())[3], "C4")

    def test_c4_ignores_derived_values_outside_the_two_scoped_files(self):
        sha = self.fx.commit({"README.md": "commit 0123456789abcdef0123456789abcdef01234567 pid=99999\n"})
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_c4_ignores_derived_values_outside_the_k9_row(self):
        text = instance_map() + "\nnote 0123456789abcdef0123456789abcdef01234567\n"
        sha = self.fx.commit({"agents/factory-kernel-instance.md": text})
        p = self.fx.k9("tree", sha)
        self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_t6_case_variant_spine_path_is_red(self):
        blob = self.fx.git("hash-object", "-w", "--stdin", input=b"variant\n").stdout.decode().strip()
        idx = self.fx.root / "alt-index"
        env = {"GIT_INDEX_FILE": str(idx)}
        self.fx.git("read-tree", "HEAD", extra_env=env)
        self.fx.git("-c", "core.ignorecase=false", "update-index", "--add", "--cacheinfo",
                    f"100644,{blob},Agents/Factory-Kernel-Instance.md", extra_env=env)
        tree = self.fx.out("write-tree") if False else self.fx.git("write-tree", extra_env=env).stdout.decode().strip()
        commit = self.fx.out("commit-tree", tree, "-p", "HEAD", "-m", "variant")
        p = self.fx.k9("tree", commit)
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        verdict, _, _, failed = parse_verdict(p.stdout.decode())
        self.assertEqual(verdict, "RED")
        self.assertIn("C1", failed.split(","))
        self.assertIn("case-variant", p.stdout.decode())

    def test_symlink_spine_is_not_a_blob(self):
        blob = self.fx.git("hash-object", "-w", "--stdin", input=b"CLAUDE.md").stdout.decode().strip()
        idx = self.fx.root / "alt-index2"
        env = {"GIT_INDEX_FILE": str(idx)}
        self.fx.git("read-tree", "HEAD", extra_env=env)
        self.fx.git("update-index", "--cacheinfo", f"120000,{blob},docs/ROTATION.md", extra_env=env)
        tree = self.fx.git("write-tree", extra_env=env).stdout.decode().strip()
        commit = self.fx.out("commit-tree", tree, "-p", "HEAD", "-m", "symlink")
        p = self.fx.k9("tree", commit)
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        self.assertIn("C1", parse_verdict(p.stdout.decode())[3].split(","))

    def test_t7_unreadable_objects_exit_2_never_red_or_pass(self):
        # (a) an object that never existed
        p = self.fx.k9("tree", "1234567890123456789012345678901234567890")
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[:2], ("UNKNOWN", "none"))
        # (b) a commit whose tree object was removed
        commit = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "edit\n"})
        tree = self.fx.out("rev-parse", "HEAD^{tree}")
        self.fx.remove_loose(tree)
        p = self.fx.k9("tree", commit)
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")

    def test_t7_readable_tree_with_a_missing_spine_blob_exits_2(self):
        commit = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "unique blob\n"})
        blob = self.fx.out("rev-parse", "HEAD:docs/ROTATION.md")
        self.fx.remove_loose(blob)
        p = self.fx.k9("tree", commit)
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[:2], ("UNKNOWN", "none"))

    def test_usage_errors_exit_2(self):
        for args in (("tree",), ("tree", "-x"), ("bogus-mode",), ()):
            with self.subTest(args=args):
                p = self.fx.k9(*args)
                self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
                self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")

    def test_not_a_repository_exits_2(self):
        empty = self.fx.root / "not-a-repo"
        empty.mkdir()
        p = self.fx.k9("tree", "HEAD", "--repo", str(empty), cwd=empty)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")


class PushTests(K9Base):
    def test_t1_red_tip_is_refused_and_remote_is_unchanged(self):
        self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        p = self.fx.push("master")
        self.assertNotEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), self.base)

    def test_t2_fix_forward_pushes_and_red_commit_in_range_does_not_block(self):
        self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")}, "red")
        self.assertNotEqual(self.fx.push("master").returncode, 0)
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), self.base)
        green = self.fx.commit({"agents/factory-kernel-instance.md": instance_map()}, "fix forward")
        p = self.fx.push("master")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), green)

    def test_t3_green_tip_with_red_uncommitted_worktree_passes(self):
        tip = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        self.fx.write("agents/factory-kernel-instance.md", instance_map(mech="`tools/nope-missing.py`"))
        self.assertTrue(self.fx.out("status", "--porcelain"), "worktree must be dirty for this test")
        p = self.fx.push("master")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), tip)

    def test_t4_red_tip_with_worktree_restored_to_green_is_refused(self):
        self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        self.fx.write("agents/factory-kernel-instance.md", instance_map())
        p = self.fx.push("master")
        self.assertNotEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), self.base)

    def test_t8_stale_branch_lacking_spine_and_touching_none_passes(self):
        b0 = self.fx.out("rev-parse", "HEAD~1")
        self.fx.git("checkout", "-q", "-b", "stale", b0)
        tip = self.fx.commit({"unrelated.txt": "x\n"})
        p = self.fx.push("stale")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/stale"), tip)

    def test_push_touching_no_spine_passes_even_when_inherited_tip_is_red(self):
        red = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        self.fx.git("push", "-q", "--no-verify", "origin", "master")
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), red)
        tip = self.fx.commit({"unrelated.txt": "y\n"})
        p = self.fx.push("master")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), tip)

    def test_editing_the_witness_itself_makes_the_push_touching(self):
        self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        self.fx.git("push", "-q", "--no-verify", "origin", "master")
        self.fx.commit({"tools/repo_hygiene/k9_witness.py": "# edited\n"})
        p = self.fx.push("master")
        self.assertNotEqual(p.returncode, 0, "red inherited tip must be judged when the witness file is touched")

    def test_t9_deletion_push_passes(self):
        self.fx.git("push", "-q", "--no-verify", "origin", "master:refs/heads/other")
        self.assertIsNotNone(self.fx.remote_ref("refs/heads/other"))
        p = self.fx.push(":refs/heads/other")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertIsNone(self.fx.remote_ref("refs/heads/other"))

    def _append_probe(self) -> Path:
        probe = self.fx.root / "probe.txt"
        hook = self.fx.hook_path()
        hook.write_bytes(hook.read_bytes() + f"cat > '{probe.as_posix()}'\n".encode())
        return probe

    def test_t10_stdin_reaches_a_later_hook_layer(self):
        probe = self._append_probe()
        tip = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        p = self.fx.push("master")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(
            probe.read_bytes().decode(),
            f"refs/heads/master {tip} refs/heads/master {self.base}\n",
        )

    def test_t10_negative_control_without_the_restore_the_probe_is_starved(self):
        hook = self.fx.hook_path()
        text = hook.read_bytes().decode()
        stripped = text.replace("exec 0<<K9EOF\n$K9_REFS\nK9EOF\n", "")
        self.assertNotEqual(stripped, text, "the installed block must contain the stdin restore")
        hook.write_bytes(stripped.encode())
        probe = self._append_probe()
        self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        p = self.fx.push("master")
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(probe.read_bytes(), b"", "without the hand-off the later layer must see no stdin")

    def test_t13_unreadable_pinned_witness_ref_refuses_the_push(self):
        self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        p = self.fx.push("master", extra_env={"MLV_K9_WITNESS_REF": "refs/remotes/nowhere/master"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("fetch fork", p.stderr.decode())
        self.assertEqual(self.fx.remote_ref("refs/heads/master"), self.base)


class PrePushModeTests(K9Base):
    def _line(self, local: str, remote: str, ref: str = "refs/heads/master") -> str:
        return f"{ref} {local} {ref} {remote}\n"

    def test_t7_local_commit_object_removed_exits_2(self):
        commit = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "edit\n"})
        self.fx.remove_loose(commit)
        p = self.fx.k9("pre-push", "origin", "url", input=self._line(commit, self.base))
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")

    def test_t7_new_ref_with_removed_local_object_exits_2(self):
        commit = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "edit\n"})
        self.fx.remove_loose(commit)
        p = self.fx.k9("pre-push", "origin", "url", input=self._line(commit, "0" * 40))
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())

    def test_t12_malformed_line_exits_2_and_blank_lines_are_noops(self):
        p = self.fx.k9("pre-push", "origin", "url", input="this is not a ref line\n")
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")
        for blank in ("", "\n", "\n\n", "  \n"):
            with self.subTest(blank=blank):
                p = self.fx.k9("pre-push", "origin", "url", input=blank)
                self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
                self.assertEqual(parse_verdict(p.stdout.decode()), ("PASS", "none", 0, "none"))

    def test_malformed_sha_field_exits_2(self):
        p = self.fx.k9("pre-push", "origin", "url", input="refs/heads/m nothex refs/heads/m " + "0" * 40 + "\n")
        self.assertEqual(p.returncode, 2)

    def test_stdin_is_not_required_to_end_in_a_newline(self):
        red = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        p = self.fx.k9("pre-push", "origin", "url", input=self._line(red, self.base).rstrip("\n"))
        self.assertEqual(p.returncode, 1, p.stdout.decode())

    def test_remote_sha_not_a_local_object_counts_as_touching(self):
        red = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        self.fx.commit({"unrelated.txt": "z\n"})
        tip = self.fx.out("rev-parse", "HEAD")
        # red tip inherited; range would be untouched, but the remote sha is unknown locally
        p = self.fx.k9("pre-push", "origin", "url", input=self._line(tip, "a" * 40))
        self.assertEqual(p.returncode, 1, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "RED")
        self.assertNotEqual(red, tip)

    def test_worst_verdict_wins_across_refs(self):
        green = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        red = self.fx.commit({"agents/factory-kernel-instance.md": instance_map(mech="`tools/nope-missing.py`")})
        stdin = self._line(green, self.base, "refs/heads/a") + self._line(red, self.base, "refs/heads/b")
        p = self.fx.k9("pre-push", "origin", "url", input=stdin)
        self.assertEqual(p.returncode, 1, p.stdout.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "RED")

    def test_pre_push_never_reads_the_working_tree(self):
        tip = self.fx.commit({"docs/ROTATION.md": ROTATION_MD + "green edit\n"})
        (self.fx.work / "CLAUDE.md").unlink()
        p = self.fx.k9("pre-push", "origin", "url", input=self._line(tip, self.base))
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="k9i-"))
        self.addCleanup(_rmtree, self._tmp)
        assert _TEMPLATE is not None
        self.fx = Fixture(self._tmp)
        self.repo = self._tmp / "inst"
        self.fx.git("init", "-q", "-b", "master", str(self.repo), cwd=self._tmp)
        self.fx.work = self.repo
        self.hook = self.fx.hook_path()
        # git init copies sample hooks only (never an active pre-push)
        self.assertFalse(self.hook.exists())

    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return self.fx.k9("install", "--repo", str(self.repo), "--ref", PINNED_REF, *extra, cwd=self._tmp)

    def test_t11_absent_creates_lf_only_block_under_shebang(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        data = self.hook.read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.startswith(b"#!/bin/sh\n# >>> mlv-k9-witness (managed) >>>\n"), data[:80])
        self.assertIn(b"fetch fork", data)
        self.assertIn(b"exec 0<<K9EOF\n$K9_REFS\nK9EOF\n", data)
        self.assertIn(b"MLV_K9_WITNESS_REF", data)
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "PASS")

    def test_t11_present_is_idempotent(self):
        self.install()
        before = self.hook.read_bytes()
        p = self.install()
        self.assertEqual(p.returncode, 0)
        self.assertIn("present", p.stdout.decode())
        self.assertEqual(self.hook.read_bytes(), before)

    def test_t11_older_block_is_migrated_in_place_keeping_the_tail(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_bytes(
            b"#!/bin/sh\n# >>> mlv-k9-witness (managed) >>>\necho old-version\n# <<< mlv-k9-witness <<<\necho tail\n"
        )
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertIn("migrated", p.stdout.decode())
        data = self.hook.read_bytes()
        self.assertNotIn(b"old-version", data)
        self.assertTrue(data.endswith(b"# <<< mlv-k9-witness <<<\necho tail\n"))
        self.assertEqual(data.count(b"# >>> mlv-k9-witness"), 1)
        self.assertNotIn(b"\r", data)

    def test_t11_stdin_reader_above_our_block_is_refused_and_file_untouched(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        original = (
            b"#!/bin/sh\nPAYLOAD=$(cat)\n# >>> mlv-k9-witness (managed) >>>\necho x\n# <<< mlv-k9-witness <<<\n"
        )
        self.hook.write_bytes(original)
        p = self.install()
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertIn("not directly under", p.stdout.decode() + p.stderr.decode())
        self.assertEqual(self.hook.read_bytes(), original)
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")

    def test_foreign_stdin_reader_without_our_block_is_refused(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        for reader in (b'git lfs pre-push "$@"\n', b"while read a b c d; do :; done\n", b"cat > /dev/null\n"):
            with self.subTest(reader=reader):
                original = b"#!/bin/sh\n" + reader
                self.hook.write_bytes(original)
                p = self.install()
                self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
                self.assertIn("stdin reader", p.stdout.decode() + p.stderr.decode())
                self.assertEqual(self.hook.read_bytes(), original)

    def test_foreign_hook_without_readers_gets_block_directly_under_shebang(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_bytes(b"#!/bin/sh\n# a comment mentioning read and cat only\necho existing\n")
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        data = self.hook.read_bytes()
        self.assertTrue(data.startswith(b"#!/bin/sh\n# >>> mlv-k9-witness (managed) >>>\n"))
        self.assertTrue(data.endswith(b"# <<< mlv-k9-witness <<<\n# a comment mentioning read and cat only\necho existing\n"))

    def test_crlf_or_non_shell_hooks_are_refused(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        for original in (b"#!/bin/sh\r\necho x\r\n", b"#!/usr/bin/env python3\nprint(1)\n", b"echo no shebang\n"):
            with self.subTest(original=original):
                self.hook.write_bytes(original)
                p = self.install()
                self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
                self.assertEqual(self.hook.read_bytes(), original)

    def test_dry_run_writes_nothing(self):
        p = self.install("--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertFalse(self.hook.exists())
        self.assertIn("dry-run", p.stdout.decode())

    def test_bad_ref_characters_are_refused(self):
        p = self.fx.k9("install", "--repo", str(self.repo), "--ref", "x\"; rm -rf /; \"", cwd=self._tmp)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.hook.exists())

    def test_uninstall_removes_only_our_block(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_bytes(b"#!/bin/sh\necho existing\n")
        self.assertEqual(self.install().returncode, 0)
        p = self.fx.k9("uninstall", "--repo", str(self.repo), cwd=self._tmp)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(self.hook.read_bytes(), b"#!/bin/sh\necho existing\n")

    def test_uninstall_deletes_a_hook_that_only_held_our_block(self):
        self.assertEqual(self.install().returncode, 0)
        p = self.fx.k9("uninstall", "--repo", str(self.repo), cwd=self._tmp)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(self.hook.exists())

    def test_uninstall_when_absent_is_a_noop(self):
        p = self.fx.k9("uninstall", "--repo", str(self.repo), cwd=self._tmp)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())

    def test_install_honours_core_hooks_path(self):
        alt = self._tmp / "alt-hooks"
        alt.mkdir()
        self.fx.git("config", "core.hooksPath", str(alt))
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertTrue((alt / "pre-push").exists())

    def test_unreadable_hook_path_is_unknown_exit_2_not_a_traceback(self):
        # fable H2: an OSError while reading the hook used to exit 1 (RED's code) with no verdict line
        self.hook.mkdir(parents=True)
        p = self.install()
        self.assertEqual(p.returncode, 2, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(parse_verdict(p.stdout.decode())[0], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
