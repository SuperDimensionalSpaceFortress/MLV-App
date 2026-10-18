"""Contract tests for the tracked CoS open-ask scanner (HUB-TOOLS-TRACKED-1 r1).

tools/coordination/hub/cos-open-ask-scan.ps1 is the tracked, profile-driven copy of the hub's
board-local scanner. Every scenario here runs against fixtures and profiles written under a temp
dir; the real bus, the real board and GitHub are never read. The script under test is the tracked
copy unless HUB_COS_SCAN_SCRIPT names another one (the RED run points it at the pre-profile copy).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HUB = ROOT / 'tools' / 'coordination' / 'hub'
TRACKED_SCRIPT = HUB / 'cos-open-ask-scan.ps1'
SUITE = HUB / 'Test-CosOpenAskScan.ps1'
PROFILE = HUB / 'hub-profile.json'

PWSH = ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']
SUBPROCESS_TIMEOUT = 900

# The number of assertions the live suite passed when the scanner was ported (pinned in the run receipt).
LIVE_PASSED = 157

REPO = 'SuperDimensionalSpaceFortress/MLV-App'
GREEN_FIXTURE = (
    'schema: cos-feedback.v1\n'
    'project: mlv-app\n'
    f'repo: {REPO}\n'
    'pr: {pr}\n'
    'head: bb29c1abe84827e7f1baf96f9d05e5470f64249e\n'
    'reviewed_utc: 2026-10-13T13:58:00Z\n'
    'event: pr-opened\n'
    'verdict: merge-when-ci-green\n'
    '\n'
    '## Notes\n'
    '\n'
    'Nothing to answer.\n'
)

EXPECTED_PROFILE = {
    'schema': 'hub-profile.v1',
    'project': 'mlv-app',
    'boardRoot': 'C:\\!Layi Wkspc\\MLV-App',
    'repoSlug': 'SuperDimensionalSpaceFortress/MLV-App',
    'repoAliases': ['layibabalola/MLV-App'],
    'ownerLogin': 'layibabalola',
    'busRoot': 'C:\\!Layi Wkspc\\softwarefactory-fleet-doctrine',
    'busRef': 'origin/master',
    'cosFeedbackPath': 'cos-feedback/mlv-app',
    'cosOpenAsksOutFile': '.claude-state/heartbeat/cos-open-asks.md',
}


def script_under_test() -> Path:
    override = os.environ.get('HUB_COS_SCAN_SCRIPT')
    return Path(override) if override else TRACKED_SCRIPT


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='hub-cos-scan-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = script_under_test()

    def write_profile(self, name='profile.json', **overrides):
        data = dict(EXPECTED_PROFILE)
        data['boardRoot'] = str(self.tmp / 'board')
        data.update(overrides)
        drop = [k for k, v in overrides.items() if v is None]
        for k in drop:
            data.pop(k)
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def fixture_dir(self, header_repo=None, pr=77):
        d = self.tmp / 'fixtures'
        d.mkdir(exist_ok=True)
        text = GREEN_FIXTURE.format(pr=pr)
        if header_repo:
            text = text.replace(f'repo: {REPO}', f'repo: {header_repo}')
        (d / f'pr-{pr}.md').write_text(text, encoding='utf-8', newline='\n')
        return d

    def scan(self, *args):
        # A script with no -ProfilePath parameter silently ignores it (no CmdletBinding), so an unbound -OutFile would fall
        # back to that script's own literal default, a real board path. Refuse before running anything.
        if '-ProfilePath' in args and 'ProfilePath' not in self.script.read_text(encoding='utf-8'):
            self.fail(f'{self.script} has no -ProfilePath parameter; not run (its defaults name real board paths)')
        cmd = [*PWSH, '-File', str(self.script), *args]
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=SUBPROCESS_TIMEOUT, cwd=str(self.tmp))
        return r.returncode, r.stdout, r.stderr

    def explicit_args(self, fixtures):
        """Every profile-backed parameter passed, so the profile is never consulted."""
        return [
            '-BusRoot', str(self.tmp / 'no-bus'), '-Ref', 'origin/master', '-Repo', REPO,
            '-OwnerLogin', 'layibabalola', '-OutFile', '', '-FeedbackPath', 'cos-feedback/mlv-app',
            '-RepoAlias', 'layibabalola/MLV-App', '-FixtureDir', str(fixtures),
        ]


class TestPortedSuite(ScanCase):
    def test_g1_ported_suite_green(self):
        r = subprocess.run(
            [*PWSH, '-File', str(SUITE), '-ScriptPath', str(self.script),
             '-WorkRoot', str(self.tmp / 'suite-work')],
            text=True, capture_output=True, timeout=SUBPROCESS_TIMEOUT, cwd=str(self.tmp),
            # r2: the suite must never depend on a live gh. An unusable token makes any stray gh call fail the same way on every machine
            # (a dev box that is logged in used to hide two rows that read real PRs #301 / #303 live and went red on hosted CI).
            env={**os.environ, 'GH_TOKEN': 'invalid-token-hermetic-suite', 'GITHUB_TOKEN': 'invalid-token-hermetic-suite'})
        m = re.search(r'^TESTS passed=(\d+) failed=0\s*$', r.stdout, re.M)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-500:])
        self.assertIsNotNone(m, r.stdout[-2000:])
        self.assertGreaterEqual(int(m.group(1)), LIVE_PASSED)


class TestProfileSeam(ScanCase):
    def test_g2_profile_drives_the_repo(self):
        profile = self.write_profile(repoSlug='other/Repo')
        fx = self.fixture_dir(header_repo=REPO, pr=77)
        # ownerLogin etc. come from the profile too; -OutFile '' keeps the run file-free
        code, out, err = self.scan('-FixtureDir', str(fx), '-ProfilePath', str(profile), '-OutFile', '')
        self.assertEqual(code, 20, out + err)
        self.assertRegex(out, r'(?m)^UNKNOWN pr=77 reason=header-repo-mismatch .*scan_repo=other/Repo')

    def test_g3_unreadable_profile_with_unbound_param(self):
        missing = self.tmp / 'absent.json'
        code, out, err = self.scan('-FixtureDir', str(self.fixture_dir()), '-ProfilePath', str(missing), '-OutFile', '')
        self.assertEqual(code, 20, out + err)
        self.assertIn(f'UNKNOWN pr=- reason=hub-profile-unreadable path={missing}', out.splitlines())
        self.assertIn('COS_OPEN_ASKS unanswered=0 answered=0 skipped=0 unknown=1', out.splitlines())

    def test_g3_explicit_params_never_read_the_profile(self):
        missing = self.tmp / 'absent.json'
        fx = self.fixture_dir()
        code, out, err = self.scan(*self.explicit_args(fx), '-ProfilePath', str(missing))
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r'(?m)^NOASK pr=77 verdict=merge-when-ci-green')
        self.assertNotIn('hub-profile', out)

    def test_g4_profile_missing_owner_login(self):
        profile = self.write_profile(ownerLogin=None)
        code, out, err = self.scan('-FixtureDir', str(self.fixture_dir()), '-ProfilePath', str(profile), '-OutFile', '')
        self.assertEqual(code, 20, out + err)
        self.assertIn('UNKNOWN pr=- reason=hub-profile-missing-key key=ownerLogin', out.splitlines())

    def test_g5_bound_empty_outfile_writes_no_file(self):
        profile = self.write_profile()
        board = self.tmp / 'board'
        target = board / '.claude-state' / 'heartbeat' / 'cos-open-asks.md'
        fx = self.fixture_dir()

        code, out, err = self.scan('-FixtureDir', str(fx), '-ProfilePath', str(profile), '-OutFile', '')
        self.assertEqual(code, 0, out + err)
        self.assertFalse(board.exists(), 'a bound -OutFile "" must not create the profile boardRoot')

        code, out, err = self.scan('-FixtureDir', str(fx), '-ProfilePath', str(profile))
        self.assertEqual(code, 0, out + err)
        self.assertTrue(target.is_file(), 'an unbound -OutFile writes boardRoot/cosOpenAsksOutFile')
        self.assertIn('COS_OPEN_ASKS unanswered=0', target.read_text(encoding='utf-8'))

    def test_g6_tracked_profile_holds_the_live_defaults(self):
        self.assertEqual(json.loads(PROFILE.read_text(encoding='utf-8')), EXPECTED_PROFILE)


if __name__ == '__main__':
    unittest.main()
