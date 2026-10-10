"""Contract tests for the tracked fold-cadence verdict step (DOCTRINE-FOLD-CADENCE-1).

tools/coordination/autopilot/fold-cadence.ps1 prices the doctrine fold debt (it binds
`doctrine_recall.py --fold-debt`) and says whether a fold lane is due:

    dueFold = owedCount > 0 AND usageLevel == 'ok' AND (now - lastFold) >= 6 h

Every scenario here runs against a fake recall script and throwaway files under a temp dir;
the real bus, the real last-seen.json and the real fleet-runs root are never named. The rule is
tested as a pure function by dot-sourcing the script; the step is tested end to end through
`pwsh -File`, where every verdict, failures included, must exit 0.
"""
import calendar
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools' / 'coordination' / 'autopilot' / 'fold-cadence.ps1'

PWSH = ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']
NOW = '2026-10-10T12:00:00Z'
SHA = '1342569e280ce747df9af4541bdf636f96697625'
# Two owed commits: 13.26 h and 8.47 h before NOW (offsets exercise the UTC conversion).
OLD_COMMIT = '2026-10-09T17:44:31-05:00'
NEW_COMMIT = '2026-10-09T22:31:52-05:00'
LONG_AGO = '2026-10-09T00:00:00Z'


def commit(sha, when):
    return {'sha': sha, 'date': when, 'files': ['TRAPS.md'], 'subject': 'fixture'}


class CadenceWorld(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fold-cadence-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / 'verdict.json'
        self.last_seen = self.tmp / 'last-seen.json'
        self.last_seen.write_text(json.dumps({'project': 'mlv-app', 'lastSeen': SHA, 'lastSeenAt': '2026-10-09T22:44:17.837Z'}),
                                  encoding='utf-8')
        self.argv_log = self.tmp / 'argv.json'
        self.scratch_tmp = self.tmp / 'scratch-tmp'   # TEMP of the step: its redirect files must not outlive it
        self.scratch_tmp.mkdir()

    def fake_recall(self, body, name='fake_recall.py'):
        path = self.tmp / name
        path.write_text('import json, sys\n'
                        f'open({str(self.argv_log)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
                        + textwrap.dedent(body), encoding='utf-8')
        return path

    def fake_debt(self, commits):
        payload = {'count': len(commits), 'commits': commits}
        return self.fake_recall(f'print({json.dumps(payload)!r})\n')

    def step(self, recall, *extra, last_seen=True, usage='ok', last_fold=LONG_AGO, deadline=30, output=True):
        """Run fold-cadence.ps1 through -File; returns (exit code, verdict object or None, stdout)."""
        args = ['-DeadlineSec', str(deadline), '-UsageLevel', usage, '-NowUtc', NOW, '-RecallScript', str(recall)]
        if last_seen is True:
            args += ['-LastSeenPath', str(self.last_seen)]
        elif last_seen:
            args += ['-LastSeenPath', str(last_seen)]
        if last_fold:
            args += ['-LastFoldUtc', last_fold]
        if output:
            args += ['-OutputPath', str(self.out)]
        env = dict(os.environ, TEMP=str(self.scratch_tmp), TMP=str(self.scratch_tmp))
        r = subprocess.run(PWSH + ['-File', str(SCRIPT), *args, *extra], text=True, capture_output=True, env=env)
        verdict = json.loads(self.out.read_text(encoding='utf-8')) if output and self.out.exists() else None
        return r.returncode, verdict, r.stdout


class DecisionRuleTests(unittest.TestCase):
    """The rule as a pure function: dot-source the script and call Get-FoldCadenceDecision."""

    def decide(self, cases):
        calls = '; '.join(
            f"$r = Get-FoldCadenceDecision -OwedCount {owed} -UsageLevel '{usage}' -LastFoldUtc {last!r} "
            f"-NowUtc ([datetime]'{NOW}').ToUniversalTime() -MinFoldIntervalHours {hours}; "
            "$o += ,@($r.DueFold, $r.Reason)"
            for owed, usage, last, hours in cases)
        cmd = f". '{SCRIPT}'; $o = @(); {calls}; ConvertTo-Json -InputObject $o -Compress -Depth 4"
        r = subprocess.run(PWSH + ['-Command', cmd], text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_rule_table(self):
        cases = [
            # owed, usage, last fold, interval hours  ->  due, reason
            ((3, 'ok', '2026-10-09T00:00:00Z', 6), (True, 'due')),
            ((1, 'ok', '2026-10-10T06:00:00Z', 6), (True, 'due')),        # exactly 6 h: due
            ((0, 'ok', '2026-10-09T00:00:00Z', 6), (False, 'no-owed')),
            ((3, 'hold', '2026-10-09T00:00:00Z', 6), (False, 'usage-hold')),
            ((3, 'prep', '2026-10-09T00:00:00Z', 6), (False, 'usage-prep')),
            ((3, '', '2026-10-09T00:00:00Z', 6), (False, 'usage-unknown')),
            ((3, 'ok', '2026-10-10T06:00:01Z', 6), (False, 'fold-recent')),  # 1 s short of 6 h
            ((3, 'ok', '2026-10-10T11:00:00Z', 6), (False, 'fold-recent')),
            ((3, 'ok', '2026-10-10T11:00:00Z', 0.5), (True, 'due')),       # the interval is a parameter
            ((3, 'OK', '2026-10-09T00:00:00Z', 6), (True, 'due')),         # level is case-insensitive
            ((3, 'ok', '', 6), (True, 'due')),                              # no fold ever recorded
            (('$null', 'ok', '2026-10-09T00:00:00Z', 6), (False, 'owed-unknown')),
        ]
        got = self.decide([c for c, _ in cases])
        for (case, want), have in zip(cases, got):
            with self.subTest(case=case):
                self.assertEqual((have[0], have[1]), want)


class FoldCadenceStepTests(CadenceWorld):
    def test_owed_ok_usage_old_fold_is_due(self):
        code, v, stdout = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT), commit('b' * 40, NEW_COMMIT)]))
        self.assertEqual(code, 0)
        self.assertEqual(v['schema'], 'mlv-app/fold-cadence/v1')
        self.assertIs(v['dueFold'], True)
        self.assertEqual(v['reason'], 'due')
        self.assertEqual(v['owedCount'], 2)
        self.assertEqual(v['oldestOwedCommitUtc'][:19], '2026-10-09T22:44:31')
        self.assertAlmostEqual(v['oldestOwedAgeHours'], 13.26, places=2)
        self.assertEqual(v['ackedSha'], SHA)
        self.assertEqual(v['ackedAtUtc'][:19], '2026-10-09T22:44:17')
        self.assertEqual(v['lastFoldUtc'][:19], '2026-10-09T00:00:00')
        self.assertEqual(v['evaluatedAtUtc'][:19], '2026-10-10T12:00:00')
        self.assertGreaterEqual(v['elapsedSec'], 0)
        self.assertEqual(sorted(v), sorted([
            'schema', 'owedCount', 'oldestOwedCommitUtc', 'oldestOwedAgeHours', 'ackedSha', 'ackedAtUtc', 'lastFoldUtc',
            'lastFoldAgeHours', 'dueFold', 'reason', 'evaluatedAtUtc', 'elapsedSec']))
        # The step also returns the verdict to its caller.
        self.assertIn('dueFold', stdout)

    def test_cursor_and_deadline_are_passed_to_the_recall_tool(self):
        self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), '-Bus', str(self.tmp))
        argv = json.loads(self.argv_log.read_text(encoding='utf-8'))
        self.assertIn('--fold-debt', argv)
        self.assertEqual(argv[argv.index('--since') + 1], SHA)
        self.assertEqual(argv[argv.index('--bus') + 1], str(self.tmp))
        self.assertLessEqual(float(argv[argv.index('--git-timeout') + 1]), 10.0)

    def test_no_owed_is_not_due(self):
        code, v, _ = self.step(self.fake_debt([]))
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'no-owed')
        self.assertEqual(v['owedCount'], 0)
        self.assertIsNone(v['oldestOwedCommitUtc'])
        self.assertIsNone(v['oldestOwedAgeHours'])

    def test_usage_hold_is_not_due(self):
        for level in ('hold', 'prep'):
            with self.subTest(level=level):
                code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), usage=level)
                self.assertEqual(code, 0)
                self.assertIs(v['dueFold'], False)
                self.assertEqual(v['reason'], f'usage-{level}')
                self.assertEqual(v['owedCount'], 1)

    def test_fold_within_six_hours_is_not_due(self):
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), last_fold='2026-10-10T07:00:00Z')
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'fold-recent')
        self.assertAlmostEqual(v['lastFoldAgeHours'], 5.0, places=2)

    def test_min_interval_is_a_parameter(self):
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), '-MinFoldIntervalHours', '4',
                               last_fold='2026-10-10T07:00:00Z')
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], True)

    def test_timeout_is_a_typed_verdict_and_the_child_is_killed(self):
        pid_file = self.tmp / 'pid.txt'
        recall = self.fake_recall(f'import os, time\nopen({str(pid_file)!r}, "w").write(str(os.getpid()))\ntime.sleep(120)\n')
        started = time.monotonic()
        code, v, _ = self.step(recall, deadline=5)
        wall = time.monotonic() - started
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'timeout')
        self.assertIsNone(v['owedCount'])
        self.assertLess(wall, 30, 'the deadline did not bound the step')
        self.assertLess(v['elapsedSec'], 15)
        # The fake's python process must be gone (the tree is killed, not left running to the end of its sleep).
        pid = pid_file.read_text(encoding='utf-8').strip()
        for _ in range(20):
            alive = pid in subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], text=True, capture_output=True).stdout
            if not alive:
                break
            time.sleep(0.25)
        self.assertFalse(alive, f'recall child {pid} survived the deadline')

    def test_missing_last_seen_is_typed(self):
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), last_seen=str(self.tmp / 'absent.json'))
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'last-seen-missing')
        self.assertFalse(self.argv_log.exists(), 'the recall tool must not run without a cursor')

    def test_no_last_seen_parameter_is_typed(self):
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), last_seen=False)
        self.assertEqual(code, 0)
        self.assertEqual(v['reason'], 'last-seen-missing')
        self.assertIs(v['dueFold'], False)

    def test_unreadable_and_shaless_last_seen_are_typed(self):
        recall = self.fake_debt([commit('a' * 40, OLD_COMMIT)])
        cases = [('{not json', 'last-seen-unreadable'), ('[]', 'last-seen-unreadable'),
                 (json.dumps({'project': 'mlv-app'}), 'last-seen-no-sha'),
                 (json.dumps({'lastSeen': 'not-a-sha'}), 'last-seen-no-sha')]
        for text, reason in cases:
            with self.subTest(reason=reason, text=text):
                self.last_seen.write_text(text, encoding='utf-8')
                code, v, _ = self.step(recall)
                self.assertEqual(code, 0)
                self.assertIs(v['dueFold'], False)
                self.assertEqual(v['reason'], reason)

    def test_recall_failures_are_typed(self):
        cases = [
            ('import sys\nsys.exit(3)\n', 'recall-unknown-sha'),
            ('import sys\nsys.exit(4)\n', 'recall-failed-exit-4'),
            ('import sys\nsys.exit(2)\n', 'recall-failed-exit-2'),
            ('print("not json at all")\n', 'recall-output-unreadable'),
            ('print("{}")\n', 'recall-output-unreadable'),
            ('raise SystemExit("boom")\n', 'recall-failed-exit-1'),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason, body=body):
                code, v, _ = self.step(self.fake_recall(body))
                self.assertEqual(code, 0)
                self.assertIs(v['dueFold'], False)
                self.assertEqual(v['reason'], reason)
                self.assertEqual(v['ackedSha'], SHA)

    def test_missing_recall_script_and_launcher_are_typed(self):
        code, v, _ = self.step(self.tmp / 'no-such-recall.py')
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'recall-failed-exit-2')
        code, v, _ = self.step(self.fake_debt([]), '-PythonCommand', 'no-such-launcher-xyz')
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'error')

    def test_no_output_path_still_returns_the_verdict(self):
        code, v, stdout = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), output=False)
        self.assertEqual(code, 0)
        self.assertIsNone(v)
        self.assertFalse(self.out.exists())
        self.assertIn('due', stdout)

    def test_unwritable_output_path_does_not_fail_the_step(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('a file where a directory is needed', encoding='utf-8')
        r = subprocess.run(PWSH + ['-File', str(SCRIPT), '-LastSeenPath', str(self.last_seen), '-NowUtc', NOW,
                                   '-RecallScript', str(self.fake_debt([])), '-OutputPath', str(blocker / 'sub' / 'v.json')],
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_last_fold_comes_from_the_newest_fold_report_of_a_fold_run_dir(self):
        runs = self.tmp / 'fleet-runs'
        newest = runs / 'lane-DOCTRINE-FOLD-20261010-r1-20261010T0200Z'
        older = runs / 'lane-DOCTRINE-FOLD-20261009-r1-20261009T2236Z'
        cadence = runs / 'lane-DOCTRINE-FOLD-CADENCE-1-r1-20261010T1125Z'   # fold-named, but never folded
        other = runs / 'lane-SOMETHING-ELSE-r1'                              # not fold-named: never looked into
        for d in (newest, older, cadence, other):
            d.mkdir(parents=True)
        (newest / 'fold-report.md').write_text('x', encoding='utf-8')
        (older / 'fold-report.md').write_text('x', encoding='utf-8')
        (other / 'fold-note.md').write_text('x', encoding='utf-8')
        (cadence / 'summary.md').write_text('x', encoding='utf-8')
        t_new = calendar.timegm(time.strptime('2026-10-10 07:00:00', '%Y-%m-%d %H:%M:%S'))
        t_old = t_new - 86400
        t_other = t_new + 3600
        os.utime(newest / 'fold-report.md', (t_new, t_new))
        os.utime(older / 'fold-report.md', (t_old, t_old))
        os.utime(other / 'fold-note.md', (t_other, t_other))
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), '-FoldRunsRoot', str(runs), last_fold=None)
        self.assertEqual(code, 0)
        self.assertEqual(v['lastFoldUtc'][:19], '2026-10-10T07:00:00')
        self.assertAlmostEqual(v['lastFoldAgeHours'], 5.0, places=2)
        self.assertIs(v['dueFold'], False)
        self.assertEqual(v['reason'], 'fold-recent')

    def test_no_fold_run_dirs_means_no_recorded_fold(self):
        runs = self.tmp / 'fleet-runs'
        (runs / 'lane-DOCTRINE-FOLD-CADENCE-1-r1').mkdir(parents=True)
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), '-FoldRunsRoot', str(runs), last_fold=None)
        self.assertEqual(code, 0)
        self.assertIsNone(v['lastFoldUtc'])
        self.assertIs(v['dueFold'], True)

    def test_missing_fold_runs_root_is_not_an_error(self):
        code, v, _ = self.step(self.fake_debt([commit('a' * 40, OLD_COMMIT)]), '-FoldRunsRoot', str(self.tmp / 'nope'),
                               last_fold=None)
        self.assertEqual(code, 0)
        self.assertIs(v['dueFold'], True)

    def test_step_removes_its_redirect_files(self):
        for recall in (self.fake_debt([commit('a' * 40, OLD_COMMIT)]), self.fake_recall('import sys\nsys.exit(4)\n')):
            code, v, _ = self.step(recall)
            self.assertEqual(code, 0)
            self.assertIsNotNone(v, 'the step must have run and written its verdict')
            self.assertEqual(list(self.scratch_tmp.iterdir()), [])


class AsciiOnlyTests(unittest.TestCase):
    def test_script_is_ascii(self):
        SCRIPT.read_bytes().decode('ascii')


if __name__ == '__main__':
    unittest.main()
