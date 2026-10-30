"""PR-PROVIDER-MODEL-TOPOLOGY-1: the provider/model topology block is RECORD-ONLY.

Every run tree is synthetic under a temp dir; nothing reads the real fleet-runs. The contract these rows
pin: render names provider and exact model per role and binds each row to a full 40-hex SHA; splice is
idempotent and leaves bytes outside the markers alone; check always prints one line and always exits 0.
"""
import atexit
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "tools", "coordination", "pr_topology.py")
_spec = importlib.util.spec_from_file_location("pr_topology", TOOL)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)

CARD = "CARD-X-1"
PR = 700
HEAD = "a" * 40
OLD = "b" * 40
IMPL_SHA = "c" * 40
FIX_SHA = "d" * 40
EMPTY = tempfile.mkdtemp(prefix="pr-topology-empty-")  # a runs root with no run dirs
atexit.register(shutil.rmtree, EMPTY, True)


def _receipt(**kw):
    base = {"schema": "mlv-app/fleet-lane-receipt/v1", "state": "complete", "card": CARD, "effort": "high"}
    base.update(kw)
    return base


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _verdict(sha):
    return 'prose before\n```json\n%s\n```\n' % json.dumps(
        {"verdict": "APPROVE", "reviewer": "x", "subject_sha": sha, "verified_head": sha, "pr": PR})


def make_runs(root):
    """producer r1 (claude/opus), fix r2 (claude/sonnet), keys fable+sol(old) and opus key3."""
    def dirp(name):
        return os.path.join(root, name)
    _write(os.path.join(dirp("lane-%s-r1-20261024T0100Z" % CARD), "opus-001.receipt.json"),
           json.dumps(_receipt(lane="opus", engine="claude", requestedModel="opus", resolvedModel="claude-opus-5-5")))
    _write(os.path.join(dirp("lane-%s-r1-20261024T0100Z" % CARD), "summary.md"), "DONE %s PR %d | x\n" % (IMPL_SHA, PR))
    _write(os.path.join(dirp("lane-%s-r2-20261024T0200Z" % CARD), "sonnet-001.receipt.json"),
           json.dumps(_receipt(lane="sonnet", engine="claude", requestedModel="sonnet", resolvedModel="claude-sonnet-5-5")))
    _write(os.path.join(dirp("lane-%s-r2-20261024T0200Z" % CARD), "summary.md"), "DONE %s PR %d | y\n" % (FIX_SHA, PR))
    _write(os.path.join(dirp("review-pr%d-fable-r1-20261024T0300Z" % PR), "fable-001.receipt.json"),
           json.dumps(_receipt(lane="fable", engine="claude", requestedModel="fable", resolvedModel="claude-fable-5-1")))
    _write(os.path.join(dirp("review-pr%d-fable-r1-20261024T0300Z" % PR), "fable-001.last.txt"), _verdict(HEAD))
    _write(os.path.join(dirp("review-pr%d-sol-r1-20261024T0300Z" % PR), "sol-001.receipt.json"),
           json.dumps(_receipt(lane="sol", engine="codex", requestedModel="sol", resolvedModel="gpt-6.1-sol")))
    _write(os.path.join(dirp("review-pr%d-sol-r1-20261024T0300Z" % PR), "sol-001.last.txt"), _verdict(OLD))
    _write(os.path.join(dirp("review-pr%d-opus-key3-r1-20261024T0400Z" % PR), "opus-001.receipt.json"),
           json.dumps(_receipt(lane="opus", engine="claude", requestedModel="opus", resolvedModel="claude-opus-5-5")))
    _write(os.path.join(dirp("review-pr%d-opus-key3-r1-20261024T0400Z" % PR), "opus-001.last.txt"), _verdict(HEAD))
    # noise that must never appear: another PR, another card
    _write(os.path.join(dirp("review-pr701-sol-r1-20261024T0300Z"), "sol-001.receipt.json"),
           json.dumps(_receipt(lane="sol", engine="codex", resolvedModel="gpt-OTHER-PR")))
    _write(os.path.join(dirp("lane-OTHER-CARD-r1-20261024T0100Z"), "opus-001.receipt.json"),
           json.dumps(_receipt(lane="opus", engine="claude", resolvedModel="claude-OTHER-CARD")))


def rows_of(block):
    return [ln for ln in block.splitlines() if ln.startswith("| ") and not ln.startswith("| Role")]


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        make_runs(self.root)
        self.block = pt.render(PR, HEAD, CARD, self.root)

    def _row(self, needle):
        hits = [r for r in rows_of(self.block) if needle in r]
        self.assertEqual(len(hits), 1, (needle, self.block))
        return hits[0]

    def test_block_is_framed_and_titled(self):
        self.assertTrue(self.block.startswith(pt.BEGIN + "\n## Provider/model topology"))
        self.assertTrue(self.block.endswith(pt.END))
        self.assertIn("CI (GitHub Actions; not a model)", self.block)

    def test_names_every_provider_and_exact_model(self):
        self.assertIn("| Anthropic | claude-opus-5-5 |", self._row("implementer r1"))
        self.assertIn("| Anthropic | claude-sonnet-5-5 |", self._row("fixer r2"))
        self.assertIn("| Anthropic | claude-fable-5-1 |", self._row("(fable;"))
        self.assertIn("| OpenAI | gpt-6.1-sol |", self._row("(sol;"))
        key3 = self._row("review key3")
        self.assertIn("claude-opus-5-5", key3)
        self.assertIn("Claude family", key3)
        self.assertNotIn("gpt-OTHER-PR", self.block)
        self.assertNotIn("claude-OTHER-CARD", self.block)

    def test_bound_shas_and_stale_marking(self):
        self.assertIn("| %s |" % HEAD, self._row("(fable;"))
        self.assertNotIn("stale", self._row("(fable;"))
        self.assertIn("%s %s" % (OLD, pt.STALE), self._row("(sol;"))
        self.assertIn("%s %s" % (IMPL_SHA, pt.STALE), self._row("implementer r1"))
        self.assertIn("%s %s" % (FIX_SHA, pt.STALE), self._row("fixer r2"))

    def test_every_sha_in_output_is_full_40_hex(self):
        for token in re.findall(r"\b[0-9a-f]{7,}\b", self.block):
            self.assertEqual(len(token), 40, token)
        self.assertGreaterEqual(len(re.findall(r"\b[0-9a-f]{40}\b", self.block)), 5)

    def test_missing_resolved_model_renders_claimed(self):
        _write(os.path.join(self.root, "review-pr%d-luna-r1-20261024T0500Z" % PR, "luna-001.receipt.json"),
               json.dumps(_receipt(lane="luna", engine="codex", requestedModel="luna")))
        row = [r for r in rows_of(pt.render(PR, HEAD, CARD, self.root)) if "(luna;" in r][0]
        self.assertIn("claimed: luna", row)
        self.assertIn(pt.UNBOUND, row)

    def test_running_receipt_with_no_model_says_why(self):
        _write(os.path.join(self.root, "review-pr%d-luna-r1-20261024T0500Z" % PR, "luna-001.receipt.json"),
               json.dumps({"schema": "mlv-app/fleet-lane-receipt/v1", "state": "running", "lane": "luna", "card": CARD}))
        row = [r for r in rows_of(pt.render(PR, HEAD, CARD, self.root)) if "(luna;" in r][0]
        self.assertIn("unknown (lane still running; receipt incomplete)", row)

    def test_corrupt_receipt_renders_unreadable_not_crash(self):
        _write(os.path.join(self.root, "review-pr%d-astra-r1-20261024T0500Z" % PR, "astra-001.receipt.json"), "{not json")
        _write(os.path.join(self.root, "review-pr%d-luna-r1-20261024T0500Z" % PR, "luna-001.receipt.json"), "[1, 2]")
        block = pt.render(PR, HEAD, CARD, self.root)
        self.assertEqual(block.count("unknown (receipt unreadable)"), 2)

    def test_no_producer_receipt_is_unknown_and_footer_is_unverified(self):
        with tempfile.TemporaryDirectory() as empty:
            bare = pt.render(PR, HEAD, CARD, empty)
            self.assertIn("| implementer | unknown | unknown (no receipt found) |", bare)
            foot = pt.render(PR, HEAD, CARD, empty, footers=["implementer=Claude Sonnet 5.5"])
            self.assertIn("claimed by footer, unverified: Claude Sonnet 5.5", foot)
            self.assertNotIn("no receipt found", foot)

    def test_verifier_and_execution_roles_from_receipt(self):
        _write(os.path.join(self.root, "design-%s-20261024T0050Z" % CARD, "astra-001.receipt.json"),
               json.dumps(_receipt(lane="astra", engine="codex", resolvedModel="gpt-6-astra", role="test-designer-verifier")))
        _write(os.path.join(self.root, "lane-%s-r3-20261024T0600Z" % CARD, "luna-001.receipt.json"),
               json.dumps(_receipt(lane="luna", engine="codex", resolvedModel="gpt-6-luna", role="execution-agent")))
        block = pt.render(PR, HEAD, CARD, self.root)
        self.assertIn("verifier/test designer", block)
        self.assertIn("execution agent", block)

    def test_render_is_deterministic(self):
        self.assertEqual(self.block, pt.render(PR, HEAD, CARD, self.root))

    def test_short_head_is_rejected(self):
        with self.assertRaises(ValueError):
            pt.render(PR, "abc123", CARD, self.root)


class SpliceTests(unittest.TestCase):
    def setUp(self):
        self.block = pt.render(PR, HEAD, CARD, EMPTY).encode("utf-8")

    def test_append_when_absent(self):
        out = pt.splice(b"# Title\n\nbody text\n", self.block)
        self.assertTrue(out.startswith(b"# Title\n\nbody text\n"))
        self.assertEqual(out.count(pt.BEGIN.encode()), 1)
        self.assertTrue(out.endswith(pt.END.encode() + b"\n"))

    def test_append_to_empty_body(self):
        self.assertTrue(pt.splice(b"", self.block).startswith(pt.BEGIN.encode()))

    def test_replace_when_present_keeps_outside_bytes(self):
        before, after = b"head \xc3\xa9 text\r\n\r\n", b"\r\n\r\ntail  \t\r\nno newline"
        body = before + pt.BEGIN.encode() + b"\nOLD\n" + pt.END.encode() + after
        out = pt.splice(body, self.block)
        self.assertTrue(out.startswith(before))
        self.assertTrue(out.endswith(after))
        self.assertNotIn(b"OLD", out)
        self.assertEqual(out.count(pt.BEGIN.encode()), 1)

    def test_idempotent(self):
        once = pt.splice(b"intro\n", self.block)
        self.assertEqual(pt.splice(once, self.block), once)
        crlf = pt.splice(b"intro\r\n", self.block)
        self.assertEqual(pt.splice(crlf, self.block), crlf)
        self.assertNotIn(b"\r\r", crlf)
        self.assertEqual(crlf.count(b"\n"), crlf.count(b"\r\n"))

    def test_refreshing_a_new_block_changes_only_the_block(self):
        newer = self.block.replace(HEAD.encode(), OLD.encode())
        first = pt.splice(b"A\n", self.block)
        second = pt.splice(first, newer)
        self.assertTrue(second.startswith(b"A\n"))
        self.assertIn(OLD.encode(), second)
        self.assertNotIn(HEAD.encode(), second)


class CheckTests(unittest.TestCase):
    def _body(self, head):
        return pt.splice(b"intro\n", pt.render(PR, head, CARD, EMPTY).encode("utf-8"))

    def test_ok(self):
        self.assertEqual(pt.check(self._body(HEAD), HEAD), "PR_TOPOLOGY ok")

    def test_missing(self):
        self.assertEqual(pt.check(b"just a body", HEAD), "PR_TOPOLOGY WARN missing")
        self.assertEqual(pt.check(b"", HEAD), "PR_TOPOLOGY WARN missing")

    def test_stale(self):
        self.assertEqual(pt.check(self._body(OLD), HEAD), "PR_TOPOLOGY WARN stale head=aaaaaaaa section=bbbbbbbb")

    def test_unreadable(self):
        self.assertTrue(pt.check(b"\xff\xfe", HEAD).startswith("PR_TOPOLOGY WARN unreadable"))
        self.assertTrue(pt.check(self._body(HEAD), "short").startswith("PR_TOPOLOGY WARN unreadable"))
        no_head = (pt.BEGIN + "\ntext\n" + pt.END).encode()
        self.assertTrue(pt.check(no_head, HEAD).startswith("PR_TOPOLOGY WARN unreadable"))


class AdversarialTests(unittest.TestCase):
    """FACTORY-ESCAPE discipline: malformed markers combined with a stale head must never read as ok."""

    def setUp(self):
        self.block = pt.render(PR, OLD, CARD, EMPTY).encode("utf-8")
        self.good = pt.splice(b"keep me\n", self.block)

    def test_two_begin_markers_plus_stale_head(self):
        body = self.good + b"\n" + pt.BEGIN.encode() + b"\nstray\n"
        line = pt.check(body, HEAD)
        self.assertTrue(line.startswith("PR_TOPOLOGY WARN"), line)
        self.assertNotEqual(line, "PR_TOPOLOGY ok")
        with self.assertRaises(ValueError):
            pt.splice(body, pt.render(PR, HEAD, CARD, EMPTY).encode("utf-8"))

    def test_begin_without_end_plus_stale_head(self):
        body = b"prose\n" + pt.BEGIN.encode() + b"\n" + pt.HEAD_PREFIX.encode() + OLD.encode() + b" -->\nmore prose\n"
        self.assertTrue(pt.check(body, HEAD).startswith("PR_TOPOLOGY WARN unreadable"))
        with self.assertRaises(ValueError):
            pt.splice(body, self.block)

    def test_end_before_begin(self):
        body = (pt.END + "\n" + pt.BEGIN + "\n").encode()
        self.assertTrue(pt.check(body, OLD).startswith("PR_TOPOLOGY WARN unreadable"))

    def test_duplicate_head_line_in_one_block_is_not_ok(self):
        dup = self.good.replace(pt.END.encode(), (pt.HEAD_PREFIX + HEAD + " -->\n" + pt.END).encode())
        self.assertTrue(pt.check(dup, HEAD).startswith("PR_TOPOLOGY WARN"))

    def test_refused_splice_through_cli_writes_nothing_and_loses_no_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = os.path.join(tmp, "body.md")
            blk = os.path.join(tmp, "block.md")
            out = os.path.join(tmp, "out.md")
            raw = b"line1\n" + pt.BEGIN.encode() + b"\nline2\n"
            with open(body, "wb") as fh:
                fh.write(raw)
            with open(blk, "wb") as fh:
                fh.write(self.block)
            proc = subprocess.run([sys.executable, TOOL, "splice", "--body-file", body, "--block-file", blk,
                                   "--out", out], capture_output=True)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(out))
            with open(body, "rb") as fh:
                self.assertEqual(fh.read(), raw)


class CliTests(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run([sys.executable, TOOL] + list(argv), capture_output=True, text=True)

    def test_check_always_exits_zero_with_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.md")
            with open(good, "wb") as fh:
                fh.write(pt.splice(b"x\n", pt.render(PR, HEAD, CARD, tmp).encode("utf-8")))
            plain = os.path.join(tmp, "plain.md")
            with open(plain, "w") as fh:
                fh.write("nothing here")
            binary = os.path.join(tmp, "bin.md")
            with open(binary, "wb") as fh:
                fh.write(b"\xff\xfe\x00")
            cases = [(good, HEAD, "PR_TOPOLOGY ok"), (good, OLD, "PR_TOPOLOGY WARN stale head=bbbbbbbb section=aaaaaaaa"),
                     (plain, HEAD, "PR_TOPOLOGY WARN missing"),
                     (os.path.join(tmp, "absent.md"), HEAD, "PR_TOPOLOGY WARN unreadable"),
                     (binary, HEAD, "PR_TOPOLOGY WARN unreadable"), (good, "nothex", "PR_TOPOLOGY WARN unreadable")]
            for path, head, want in cases:
                proc = self._run("check", "--body-file", path, "--head", head)
                self.assertEqual(proc.returncode, 0, (path, head, proc.stderr))
                self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stdout)
                self.assertTrue(proc.stdout.startswith(want), (proc.stdout, want))
            for bad in (["check"], ["check", "--bogus"], ["check", "--body-file"]):
                proc = self._run(*bad)
                self.assertEqual(proc.returncode, 0, bad)
                self.assertTrue(proc.stdout.startswith("PR_TOPOLOGY WARN unreadable"), proc.stdout)

    def test_render_then_splice_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = os.path.join(tmp, "runs")
            make_runs(runs)
            body, out = os.path.join(tmp, "body.md"), os.path.join(tmp, "out.md")
            with open(body, "w", newline="") as fh:
                fh.write("## Summary\n\nhello\n")
            r = self._run("render", "--pr", str(PR), "--head", HEAD, "--card", CARD, "--runs-root", runs)
            self.assertEqual(r.returncode, 0, r.stderr)
            blk = os.path.join(tmp, "block.md")
            with open(blk, "w", newline="") as fh:
                fh.write(r.stdout)
            s = self._run("splice", "--body-file", body, "--block-file", blk, "--out", out)
            self.assertEqual(s.returncode, 0, s.stderr)
            c = self._run("check", "--body-file", out, "--head", HEAD)
            self.assertEqual(c.stdout.strip(), "PR_TOPOLOGY ok")


if __name__ == "__main__":
    unittest.main()
