"""Contract tests for the tracked product-share verdict step (PRODUCT-SHARE-BIND-1).

tools/coordination/autopilot/product-share.ps1 binds the existing Test-ProductRatioGuard.ps1 into a
beat step. Every scenario here runs a FAKE guard (a tiny .ps1 under a temp dir); the real guard, the
real board and every real worktree are never named. The real guard was measured at 529 s on the board,
which is why the step has a deadline, a cache and a detached refresh.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools' / 'coordination' / 'autopilot' / 'product-share.ps1'
PROFILE = ROOT / 'tools' / 'coordination' / 'autopilot' / 'board-profile.json'

PWSH = ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']

GUARD_HEAD = 'param([string]$RepoRoot, [string]$SourceRef)\n'
GUARD_JSON = ('@{{ schema = "mlv-app/product-ratio-guard/v1"; sourceRef = "fork/master"; productShare7d = {share}; '
              'productShareThreshold = 0.5; verdict = "{verdict}"; reasons = @({reasons}); errorCode = $null }} '
              '| ConvertTo-Json -Compress\n')


def guard_source(verdict, share, reasons='"RED_PRODUCT_SHARE"', counter=None):
    body = GUARD_HEAD
    if counter:
        body += f'Add-Content -LiteralPath "{counter}" -Value "run"\n'
    return body + GUARD_JSON.format(verdict=verdict, share=share, reasons=reasons)


class ProductShareBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='product-share-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.verdict_path = self.tmp / 'state' / 'product-share-verdict.json'
        self.counter = self.tmp / 'guard-runs.txt'
        self.env = dict(os.environ)
        self.env.pop('PRODUCT_SHARE_DIR', None)

    def make_guard(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding='ascii')
        return p

    def red_guard(self):
        return self.make_guard('red.ps1', guard_source('RED', '0.26229508196721313', counter=self.counter))

    def green_guard(self):
        return self.make_guard('green.ps1', guard_source('GREEN', '0.61', reasons='', counter=self.counter))

    def guard_runs(self):
        return len(self.counter.read_text().split()) if self.counter.exists() else 0

    def run_step(self, guard, *extra, deadline=30, cards=None, timeout=120):
        args = ['-File', str(SCRIPT), '-GuardPath', str(guard), '-RepoRoot', str(self.tmp), '-SourceRef', 'fork/master',
                '-VerdictPath', str(self.verdict_path), '-ProfilePath', str(PROFILE), '-DeadlineSec', str(deadline)]
        if cards is not None:
            cards_file = self.tmp / 'cards.json'
            cards_file.write_text(json.dumps(cards), encoding='ascii')
            args += ['-CardsPath', str(cards_file)]
        args += list(extra)
        r = subprocess.run(PWSH + args, text=True, capture_output=True, env=self.env, timeout=timeout)
        self.assertEqual(r.returncode, 0, f'product-share must exit 0 whatever the verdict (bus rule B): {r.stderr}\n{r.stdout}')
        return json.loads(r.stdout.strip().splitlines()[-1])


class VerdictTests(ProductShareBase):
    def test_red(self):
        res = self.run_step(self.red_guard())
        self.assertEqual(res['verdict'], 'RED')
        self.assertEqual(res['schema'], 'mlv-app/product-share-verdict/v1')
        self.assertAlmostEqual(res['productShare'], 0.2623, places=3)
        self.assertEqual(res['threshold'], 0.5)
        self.assertEqual(res['sourceRef'], 'fork/master')
        self.assertEqual(res['cache'], 'run')
        self.assertIn('RED_PRODUCT_SHARE', res['guardReasons'])
        self.assertEqual(res['line'], 'Product share (7 d): RED 0.26 of 0.50')
        self.assertIn('evaluatedAtUtc', res)
        self.assertIn('elapsedSec', res)
        saved = json.loads(self.verdict_path.read_text(encoding='ascii'))
        self.assertEqual(saved['verdict'], 'RED')

    def test_step_attaches_the_plan_when_given_a_cards_file(self):
        cards = [{'state': 'queued', 'kind': 'product', 'touchesProduct': True}]
        res = self.run_step(self.red_guard(), cards=cards)
        self.assertTrue(res['plan']['reserveProductSlot'])
        self.assertEqual(res['plan']['maxHubToolingLanes'], 1)

    def test_green(self):
        res = self.run_step(self.green_guard())
        self.assertEqual(res['verdict'], 'GREEN')
        self.assertEqual(res['line'], 'Product share (7 d): GREEN 0.61 of 0.50')

    def test_timeout_is_unknown_not_red(self):
        guard = self.make_guard('sleeper.ps1', GUARD_HEAD + 'Start-Sleep -Seconds 60\n')
        started = time.monotonic()
        res = self.run_step(guard, deadline=3)
        self.assertLess(time.monotonic() - started, 40, 'the step must not wait for the guard past its deadline')
        self.assertEqual(res['verdict'], 'UNKNOWN')
        self.assertIn('guard-timeout', res['reason'])
        self.assertFalse(self.verdict_path.exists(), 'an UNKNOWN result must not be stored as a definitive verdict')

    def test_guard_error_json_is_unknown(self):
        guard = self.make_guard('errjson.ps1', GUARD_HEAD +
                                '@{ verdict = "ERROR"; errorCode = "ERROR_REF_UNRESOLVED"; reasons = @("ERROR_REF_UNRESOLVED") } | ConvertTo-Json -Compress\nexit 3\n')
        res = self.run_step(guard)
        self.assertEqual(res['verdict'], 'UNKNOWN')
        self.assertIn('ERROR_REF_UNRESOLVED', res['reason'])

    def test_guard_garbage_and_throw_are_unknown(self):
        for name, text in (('garbage.ps1', GUARD_HEAD + 'Write-Output "boom"\nexit 1\n'),
                           ('throws.ps1', GUARD_HEAD + 'throw "guard blew up"\n'),
                           ('empty.ps1', GUARD_HEAD + 'exit 0\n')):
            with self.subTest(guard=name):
                self.verdict_path.parent.mkdir(parents=True, exist_ok=True)
                for stale in self.verdict_path.parent.glob('*'):
                    stale.unlink()
                res = self.run_step(self.make_guard(name, text))
                self.assertEqual(res['verdict'], 'UNKNOWN')

    def test_missing_guard_is_unknown(self):
        res = self.run_step(self.tmp / 'no-such-guard.ps1')
        self.assertEqual(res['verdict'], 'UNKNOWN')


class CacheTests(ProductShareBase):
    def test_fresh_verdict_is_a_cache_hit_and_does_not_rerun_the_guard(self):
        guard = self.red_guard()
        first = self.run_step(guard)
        self.assertEqual(first['cache'], 'run')
        self.assertEqual(self.guard_runs(), 1)
        second = self.run_step(guard)
        self.assertEqual(second['cache'], 'hit')
        self.assertEqual(second['verdict'], 'RED')
        self.assertEqual(self.guard_runs(), 1, 'a cache hit must not run the guard')
        self.assertEqual(second['evaluatedAtUtc'], first['evaluatedAtUtc'])

    def test_force_reruns(self):
        guard = self.red_guard()
        self.run_step(guard)
        res = self.run_step(guard, '-Force')
        self.assertEqual(res['cache'], 'run')
        self.assertEqual(self.guard_runs(), 2)

    def test_expired_verdict_reruns(self):
        guard = self.green_guard()
        self.run_step(guard)
        saved = json.loads(self.verdict_path.read_text(encoding='ascii'))
        saved['evaluatedAtUtc'] = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        self.verdict_path.write_text(json.dumps(saved), encoding='ascii')
        res = self.run_step(guard)
        self.assertEqual(res['cache'], 'run')
        self.assertEqual(self.guard_runs(), 2)

    def test_failed_attempt_backs_off_and_keeps_the_last_verdict_out_of_the_way(self):
        sleeper = self.make_guard('sleeper.ps1', GUARD_HEAD + 'Add-Content -LiteralPath "%s" -Value "run"\nStart-Sleep -Seconds 60\n' % self.counter)
        self.run_step(sleeper, deadline=2)
        self.assertEqual(self.guard_runs(), 1)
        again = self.run_step(sleeper, deadline=2)
        self.assertEqual(again['verdict'], 'UNKNOWN')
        self.assertEqual(again['cache'], 'backoff')
        self.assertEqual(self.guard_runs(), 1, 'a recent failed attempt must not be retried on every call')

    def test_detach_serves_stale_verdict_and_refreshes_in_the_background(self):
        guard = self.green_guard()
        self.verdict_path.parent.mkdir(parents=True, exist_ok=True)
        stale = {'schema': 'mlv-app/product-share-verdict/v1', 'verdict': 'RED', 'productShare': 0.26, 'threshold': 0.5,
                 'evaluatedAtUtc': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
                 'elapsedSec': 500.0, 'sourceRef': 'fork/master', 'reason': 'guard-red', 'guardReasons': [], 'guardExit': 0}
        self.verdict_path.write_text(json.dumps(stale), encoding='ascii')
        res = self.run_step(guard, '-Detach')
        self.assertEqual(res['verdict'], 'RED', 'the last definitive verdict is served while the refresh runs')
        self.assertTrue(res['stale'])
        self.assertEqual(res['cache'], 'refreshing')
        self.assertIn('[stale]', res['line'])
        deadline = time.monotonic() + 60
        refreshed = None
        while time.monotonic() < deadline:
            saved = json.loads(self.verdict_path.read_text(encoding='ascii'))
            if saved['verdict'] == 'GREEN':
                refreshed = saved
                break
            time.sleep(0.5)
        self.assertIsNotNone(refreshed, 'the detached worker never refreshed the verdict file')
        after = self.run_step(guard, '-Detach')
        self.assertEqual(after['cache'], 'hit')
        self.assertEqual(after['verdict'], 'GREEN')

    def test_detach_with_no_verdict_is_unknown_not_red(self):
        sleeper = self.make_guard('sleeper.ps1', GUARD_HEAD + 'Start-Sleep -Seconds 60\n')
        res = self.run_step(sleeper, '-Detach', '-WorkerDeadlineSec', '2')
        self.assertEqual(res['verdict'], 'UNKNOWN')
        self.assertIn(res['cache'], ('refreshing', 'none'))


class PlanTests(unittest.TestCase):
    """Get-ProductSlotPlan is pure; the script is dot-sourced, which runs nothing."""

    def plan(self, verdict, cards):
        env = dict(os.environ, PS_SCRIPT=str(SCRIPT), PS_VERDICT=verdict, PS_CARDS=json.dumps(cards))
        cmd = (". $env:PS_SCRIPT; $c = @(ConvertFrom-Json $env:PS_CARDS); "
               "Get-ProductSlotPlan -Verdict $env:PS_VERDICT -Cards $c | ConvertTo-Json -Compress")
        r = subprocess.run(PWSH + ['-Command', cmd], text=True, capture_output=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    PRODUCT_DUE = [{'state': 'queued', 'kind': 'product', 'touchesProduct': True},
                   {'state': 'queued', 'kind': 'hub-tooling', 'touchesProduct': False}]

    def test_red_with_product_due_reserves_a_slot_and_caps_hub_tooling_at_one(self):
        p = self.plan('RED', self.PRODUCT_DUE)
        self.assertTrue(p['reserveProductSlot'])
        self.assertEqual(p['maxHubToolingLanes'], 1)
        self.assertTrue(p['mayStartHubTooling'])

    def test_one_hub_tooling_lane_running_blocks_a_second_while_red_and_product_due(self):
        cards = self.PRODUCT_DUE + [{'state': 'running', 'kind': 'incident', 'touchesProduct': False}]
        p = self.plan('RED', cards)
        self.assertEqual(p['hubToolingRunning'], 1)
        self.assertFalse(p['mayStartHubTooling'])

    def test_red_without_a_product_card_due_reserves_nothing(self):
        p = self.plan('RED', [{'state': 'queued', 'kind': 'hub-tooling', 'touchesProduct': False},
                              {'state': 'running', 'kind': 'product', 'touchesProduct': True}])
        self.assertFalse(p['reserveProductSlot'])
        self.assertIsNone(p['maxHubToolingLanes'])

    def test_a_product_label_that_touches_no_product_path_is_not_due(self):
        p = self.plan('RED', [{'state': 'queued', 'kind': 'product', 'touchesProduct': False}])
        self.assertFalse(p['reserveProductSlot'])

    def test_green_and_unknown_never_reserve_or_cap(self):
        for verdict in ('GREEN', 'UNKNOWN', 'ERROR', ''):
            with self.subTest(verdict=verdict):
                cards = self.PRODUCT_DUE + [{'state': 'running', 'kind': 'hub-tooling', 'touchesProduct': False}] * 3
                p = self.plan(verdict, cards)
                self.assertFalse(p['reserveProductSlot'])
                self.assertIsNone(p['maxHubToolingLanes'])
                self.assertTrue(p['mayStartHubTooling'])


class ExitCodeInvariantTests(ProductShareBase):
    """Bus rule B: a RED verdict never changes the exit code of a product act."""

    def run_act(self, guard, act_exit):
        env = dict(self.env, PS_SCRIPT=str(SCRIPT), PS_GUARD=str(guard), PS_REPO=str(self.tmp), PS_VERDICT=str(self.verdict_path),
                   PS_PROFILE=str(PROFILE), PS_EXE=str(shutil.which('pwsh.exe') or 'pwsh.exe'), PS_ACT_EXIT=str(act_exit))
        cmd = (". $env:PS_SCRIPT; "
               "$share = @{ GuardPath = $env:PS_GUARD; RepoRoot = $env:PS_REPO; SourceRef = 'fork/master'; VerdictPath = $env:PS_VERDICT; ProfilePath = $env:PS_PROFILE }; "
               "$code = Invoke-ProductAct -ActCommand @($env:PS_EXE, '-NoProfile', '-Command', \"exit $env:PS_ACT_EXIT\") -ShareArgs $share; "
               "exit $code")
        return subprocess.run(PWSH + ['-Command', cmd], text=True, capture_output=True, env=env, timeout=120).returncode

    def test_red_verdict_leaves_the_product_act_exit_code_unchanged(self):
        for act_exit in (0, 1, 7):
            with self.subTest(act_exit=act_exit):
                shutil.rmtree(self.verdict_path.parent, ignore_errors=True)
                self.assertEqual(self.run_act(self.red_guard(), act_exit), act_exit)
                saved = json.loads(self.verdict_path.read_text(encoding='ascii'))
                self.assertEqual(saved['verdict'], 'RED', 'the act must have run AFTER a real RED verdict')

    def test_step_itself_exits_zero_on_red_green_and_unknown(self):
        # run_step asserts returncode == 0 for each of these.
        self.run_step(self.red_guard())
        shutil.rmtree(self.verdict_path.parent, ignore_errors=True)
        self.run_step(self.green_guard())
        shutil.rmtree(self.verdict_path.parent, ignore_errors=True)
        self.run_step(self.make_guard('throws.ps1', GUARD_HEAD + 'throw "x"\n'))

    def test_act_still_runs_when_the_verdict_step_is_broken(self):
        self.assertEqual(self.run_act(self.tmp / 'no-such-guard.ps1', 5), 5)


class AsciiOnlyTests(unittest.TestCase):
    def test_script_is_ascii(self):
        SCRIPT.read_bytes().decode('ascii')


if __name__ == '__main__':
    unittest.main()
