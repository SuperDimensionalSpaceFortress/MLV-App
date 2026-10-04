"""CI must report every required check on GitHub merge-queue (merge_group) runs.

A required check that never reports on a merge_group run holds the queue until
its timeout and then ejects the pull request.  These tests parse the workflow
text (no YAML dependency in the hygiene environment) and drive the router.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from .protected_check_router import RouteError, build_receipt, main

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Branch protection pins these contexts; the merge-queue ruleset requires the same set.
REQUIRED_CHECKS = (
    "Repo Hygiene Python (windows-latest)",
    "Repo Hygiene Python (ubuntu-latest)",
    "Windows GUI Pilot",
    "Windows Product Oracles",
    "Batch Compile",
)

FULL_ROUTE_REASON = "MERGE_GROUP_RUN_REAL_ORACLES"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _trigger_block(text: str) -> str:
    match = re.search(r"^on:\s*\n((?:[ \t]+.*\n|[ \t]*\n)+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def _jobs(text: str) -> dict:
    """Map job id -> job body for the top-level `jobs:` mapping."""
    start = re.search(r"^jobs:\s*\n", text, re.MULTILINE)
    if start is None:
        return {}
    body = text[start.end():]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _job_names(job_body: str) -> set:
    """Check-run names a job reports, with a single-axis matrix expanded."""
    match = re.search(r"^    name:[ \t]*(.+?)[ \t]*$", job_body, re.MULTILINE)
    if match is None:
        return set()
    template = match.group(1).strip("'\"")
    placeholder = re.search(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", template)
    if placeholder is None:
        return {template}
    axis = re.search(
        rf"^        {placeholder.group(1)}:[ \t]*\[(.*?)\]", job_body, re.MULTILINE
    )
    if axis is None:
        return {template}
    values = [item.strip().strip("'\"") for item in axis.group(1).split(",") if item.strip()]
    return {template.replace(placeholder.group(0), value) for value in values}


def _needed_jobs(job_body: str) -> set:
    """Job ids named by a job's `needs:` (inline scalar, inline list or block list)."""
    inline = re.search(r"^    needs:[ \t]*(.+?)[ \t]*$", job_body, re.MULTILINE)
    if inline is not None:
        return {item.strip() for item in inline.group(1).strip("[]").split(",") if item.strip()}
    block = re.search(r"^    needs:[ \t]*\n((?:      - .+\n)+)", job_body, re.MULTILINE)
    if block is None:
        return set()
    return {line.strip()[2:].strip() for line in block.group(1).splitlines()}


def _producing_jobs(jobs: dict, job_id: str) -> dict:
    """Job id -> body for `job_id` and every job it transitively needs.

    A required context can be an aggregator that only reports the result of sharded
    jobs, so the jobs that actually produce or feed it are the transitive `needs`.
    """
    found = {}
    pending = [job_id]
    while pending:
        current = pending.pop()
        if current in found or current not in jobs:
            continue
        found[current] = jobs[current]
        pending.extend(_needed_jobs(jobs[current]))
    return found


def _required_check_jobs() -> dict:
    """Map required check name -> (workflow path, job id, job body)."""
    found = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = _read(workflow)
        for job_id, body in _jobs(text).items():
            for name in _job_names(body):
                if name in REQUIRED_CHECKS:
                    found[name] = (workflow, job_id, body)
    return found


class MergeQueueWorkflowTests(unittest.TestCase):
    def test_every_required_check_has_exactly_one_producing_job(self) -> None:
        jobs = _required_check_jobs()
        self.assertEqual(sorted(jobs), sorted(REQUIRED_CHECKS))

    def test_workflows_producing_required_checks_trigger_on_merge_group(self) -> None:
        for name, (workflow, _job_id, _body) in _required_check_jobs().items():
            with self.subTest(check=name):
                triggers = _trigger_block(_read(workflow))
                self.assertRegex(triggers, r"(?m)^  merge_group:")
                self.assertRegex(triggers, r"(?m)^    types:\s*\[checks_requested\]")

    def test_required_check_jobs_are_not_gated_off_merge_group(self) -> None:
        for name, (workflow, job_id, _body) in _required_check_jobs().items():
            producing = _producing_jobs(_jobs(_read(workflow)), job_id)
            for producer_id, body in producing.items():
                job_header = body.split("\n    steps:", 1)[0]
                for condition in re.findall(r"^    if:[ \t]*(.+)$", job_header, re.MULTILINE):
                    with self.subTest(check=name, job=producer_id):
                        self.assertNotIn("github.event_name", condition)
                        self.assertNotIn("pull_request", condition)

    def test_tests_workflow_concurrency_is_not_keyed_on_the_pull_request(self) -> None:
        text = _read(WORKFLOWS / "tests.yml")
        group = re.search(r"^  group:[ \t]*(.+)$", text, re.MULTILINE)
        self.assertIsNotNone(group)
        # A merge_group event has no pull_request payload; github.ref is unique per entry.
        self.assertNotIn("pull_request", group.group(1))
        self.assertIn("github.ref", group.group(1))

    def test_router_base_uses_the_merge_group_base_sha(self) -> None:
        text = _read(WORKFLOWS / "tests.yml")
        base = re.search(r"^      ROUTER_BASE:[ \t]*(.+)$", text, re.MULTILINE)
        self.assertIsNotNone(base)
        self.assertIn("github.event_name == 'merge_group'", base.group(1))
        self.assertIn("github.event.merge_group.base_sha", base.group(1))
        # The head is the merge-group commit, which is github.sha, not a PR head.
        head = re.search(r"^      ROUTER_HEAD:[ \t]*(.+)$", text, re.MULTILINE)
        self.assertIsNotNone(head)
        self.assertTrue(head.group(1).rstrip().endswith("|| github.sha }}"))

    def test_workflow_requests_the_full_route_on_merge_group(self) -> None:
        text = _read(WORKFLOWS / "tests.yml")
        self.assertIn("if ('${{ github.event_name }}' -eq 'merge_group')", text)
        self.assertIn("$forceReal = @('--merge-group')", text)
        jobs = _jobs(text)
        for check in ("Windows Product Oracles", "Windows GUI Pilot"):
            workflow, job_id, _body = _required_check_jobs()[check]
            self.assertEqual(workflow, WORKFLOWS / "tests.yml")
            producing = _producing_jobs(jobs, job_id)
            # The context may be an aggregator over sharded part jobs: every job that
            # consumes the route output must accept the merge-group reason, and at least
            # one must exist so the loop cannot pass vacuously.
            route_consumers = {
                producer_id: body
                for producer_id, body in producing.items()
                if "protected-check-route" in _needed_jobs(body)
            }
            self.assertTrue(route_consumers, check)
            for producer_id, body in route_consumers.items():
                with self.subTest(check=check, job=producer_id):
                    self.assertIn(f"'{FULL_ROUTE_REASON}'", body)
                    self.assertIn("recognized fail-closed reason", body)

    def test_product_oracles_context_is_fed_by_the_part_jobs(self) -> None:
        jobs = _jobs(_read(WORKFLOWS / "tests.yml"))
        _workflow, job_id, _body = _required_check_jobs()["Windows Product Oracles"]
        self.assertIn("windows-product-oracles-part", _producing_jobs(jobs, job_id))


class MergeGroupRouterTests(unittest.TestCase):
    def _repo(self, tmp: str, changed: str) -> tuple:
        repo = Path(tmp)

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()

        git("init", "-q")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        target = repo / changed
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
        git("add", changed)
        git("commit", "-q", "-m", "change")
        return repo, base, git("rev-parse", "HEAD")

    def test_provider_control_only_diff_is_na_outside_the_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = self._repo(tmp, "tools/provider_control/x.py")
            receipt = build_receipt(repo, base, head)
        self.assertFalse(receipt["route"]["productOraclesRequired"])
        self.assertFalse(receipt["route"]["guiPilotRequired"])

    def test_merge_group_returns_the_full_route_even_for_a_na_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = self._repo(tmp, "tools/provider_control/x.py")
            receipt = build_receipt(repo, base, head, merge_group=True)
        route = receipt["route"]
        self.assertTrue(route["productOraclesRequired"])
        self.assertTrue(route["guiPilotRequired"])
        self.assertEqual(route["reason"], FULL_ROUTE_REASON)
        self.assertEqual(route["productOracleCredit"], "RUN_REQUIRED")
        self.assertEqual(route["guiPilotCredit"], "RUN_REQUIRED")
        self.assertEqual(receipt["git"]["baseTip"], base)
        self.assertEqual(receipt["git"]["head"], head)

    def test_merge_group_without_a_base_sha_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _base, head = self._repo(tmp, "src/x.c")
            with self.assertRaises(RouteError):
                build_receipt(repo, "", head, merge_group=True)
            receipt_path = Path(tmp) / "out" / "receipt.json"
            with self.assertRaises(RouteError):
                main(
                    [
                        "--repo-root", str(repo),
                        "--head", head,
                        "--receipt", str(receipt_path),
                        "--merge-group",
                    ]
                )
            self.assertFalse(receipt_path.exists())

    def test_main_writes_the_full_route_github_output_for_merge_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = self._repo(tmp, "tools/provider_control/x.py")
            out = Path(tmp) / "out"
            out.mkdir()
            github_output = out / "github_output"
            code = main(
                [
                    "--repo-root", str(repo),
                    "--base", base,
                    "--head", head,
                    "--receipt", str(out / "receipt.json"),
                    "--github-output", str(github_output),
                    "--merge-group",
                ]
            )
            lines = github_output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(code, 0)
        self.assertIn("product=true", lines)
        self.assertIn("gui=true", lines)
        self.assertIn(f"reason={FULL_ROUTE_REASON}", lines)


if __name__ == "__main__":
    unittest.main()
