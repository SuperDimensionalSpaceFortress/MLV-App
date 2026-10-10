"""Contract tests for the tracked board fast-forward (BOARD-AUTOPILOT-TRACKED-1 slice 1).

tools/coordination/autopilot/board-ff.ps1 is the tracked, profile-driven copy of the
autopilot's board-ff. Every git scenario here runs against throwaway repos created under a
temp dir (an upstream "remote" plus a board clone); the real board and every real worktree
are never named. The only machine-global input is the process scan for a live
merge-enqueue / refresh-hook-receipt mover, and a test that would be refused by a real mover
skips instead of failing.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTOPILOT = ROOT / 'tools' / 'coordination' / 'autopilot'
BOARD_FF = AUTOPILOT / 'board-ff.ps1'
PROFILE_MODULE = AUTOPILOT / 'BoardProfile.psm1'
PROFILE = AUTOPILOT / 'board-profile.json'

PWSH = ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']


class TempWorld(unittest.TestCase):
    """An upstream repo, a board clone of it on master, and an isolated git environment."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='board-ff-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        empty_cfg = self.tmp / 'empty.gitconfig'
        empty_cfg.write_text('', encoding='utf-8')
        self.env = dict(os.environ)
        self.env.update({
            'GIT_CONFIG_GLOBAL': str(empty_cfg), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
            'GIT_TERMINAL_PROMPT': '0',
        })
        self.env.pop('BOARD_FF_SCRATCH', None)
        self.scratch = self.tmp / 'scratch'
        self.state = self.scratch / 'state.json'
        self.upstream = self.tmp / 'upstream'
        self.board = self.tmp / 'board'
        self.git(self.tmp, 'init', '-q', '-b', 'master', str(self.upstream))
        self.git(self.upstream, 'config', 'core.autocrlf', 'false')
        self.commit(self.upstream, 'README.md', 'one\n', 'first')
        self.git(self.tmp, 'clone', '-q', str(self.upstream), str(self.board))
        self.git(self.board, 'config', 'core.autocrlf', 'false')

    def git(self, cwd, *args):
        r = subprocess.run(['git', '-C', str(cwd), *args], text=True, capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0, f'git {args} failed: {r.stderr}')
        return r.stdout.strip()

    def commit(self, repo, rel, text, msg):
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8', newline='\n')
        self.git(repo, 'add', rel)
        self.git(repo, 'commit', '-q', '-m', msg)

    def head(self, repo):
        return self.git(repo, 'rev-parse', 'HEAD')

    def ff_params(self, *extra, profile=None, explicit=True, scratch=True, force=True):
        """Parameter table for board-ff.ps1; extra holds switch names such as 'WhatIf'."""
        params = {}
        if force:
            params['Force'] = True
        if explicit:
            params.update(Board=str(self.board), Remote='origin', Branch='master')
        if scratch:
            params.update(ScratchDir=str(self.scratch), StateFile=str(self.state))
        if profile:
            params['ProfilePath'] = str(profile)
        for name in extra:
            params[name] = True
        return params

    def invoke_ff(self, params, tail):
        """Run board-ff.ps1 with a hashtable splat; tail is a pwsh snippet that prints the result."""
        env = dict(self.env, BFF_SCRIPT=str(BOARD_FF), BFF_ARGS=json.dumps(params))
        cmd = '$p = ConvertFrom-Json $env:BFF_ARGS -AsHashtable; $r = & $env:BFF_SCRIPT @p; ' + tail
        r = subprocess.run(PWSH + ['-Command', cmd], text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, f'pwsh failed: {r.stderr}\n{r.stdout}')
        return r.stdout.strip().splitlines()[-1]

    def run_ff(self, *extra, **kw):
        """Run board-ff.ps1 against the board clone; returns the result object or None."""
        line = self.invoke_ff(self.ff_params(*extra, **kw),
                              "if ($null -eq $r) { 'null' } else { $r | ConvertTo-Json -Compress -EscapeHandling EscapeNonAscii }")
        return None if line == 'null' else json.loads(line)

    def assert_refused_unless_mover(self, res):
        if res and res['Result'] == 'refused' and 'mover-alive' in res['Reason']:
            self.skipTest('a real merge-enqueue/refresh-hook-receipt process is alive on this machine')


class BoardFfScenarioTests(TempWorld):
    def test_current_is_a_noop(self):
        before = self.head(self.board)
        res = self.run_ff()
        self.assertEqual(res['Result'], 'current')
        self.assertEqual(res['Reason'], 'HEAD == target')
        self.assertEqual(res['From'], res['To'])
        self.assertTrue(res['Line'].startswith('BOARD-FF current HEAD == target from='))
        self.assertEqual(self.head(self.board), before)

    def test_behind_fast_forwards(self):
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        res = self.run_ff()
        self.assert_refused_unless_mover(res)
        self.assertEqual(res['Result'], 'ok')
        self.assertEqual(res['Reason'], 'fast-forwarded to origin/master')
        self.assertEqual(self.head(self.board), self.head(self.upstream))
        self.assertEqual(res['To'], self.head(self.upstream)[:8])

    def test_whatif_reports_would_ff_and_moves_nothing(self):
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        before = self.head(self.board)
        res = self.run_ff('-WhatIf')
        self.assert_refused_unless_mover(res)
        self.assertEqual(res['Result'], 'ok')
        self.assertIn('would-ff', res['Reason'])
        self.assertEqual(self.head(self.board), before)
        self.assertFalse(self.state.exists(), '-WhatIf must not touch the state file')

    def test_dirty_tracked_is_refused(self):
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        (self.board / 'README.md').write_text('local edit\n', encoding='utf-8')
        before = self.head(self.board)
        res = self.run_ff()
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('dirty-tracked (1 path(s))'), res['Reason'])
        self.assertEqual(self.head(self.board), before)

    def test_wrong_branch_is_refused(self):
        self.git(self.board, 'checkout', '-q', '-b', 'side')
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        before = self.head(self.board)
        res = self.run_ff()
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith("wrong-branch (on 'side', need master)"), res['Reason'])
        self.assertEqual(self.head(self.board), before)

    def test_diverged_is_refused(self):
        self.commit(self.board, 'local.txt', 'mine\n', 'local')
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        before = self.head(self.board)
        res = self.run_ff()
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('diverged'), res['Reason'])
        self.assertEqual(self.head(self.board), before)

    def test_board_owned_path_change_is_refused(self):
        for rel in ('tools/hooks/h.ps1', '.claude/settings.json', 'CLAUDE.md'):
            with self.subTest(path=rel):
                self.commit(self.upstream, rel, 'changed\n', f'touch {rel}')
                before = self.head(self.board)
                res = self.run_ff()
                self.assertEqual(res['Result'], 'refused')
                self.assertTrue(res['Reason'].startswith('hook-change-pending'), res['Reason'])
                self.assertIn(rel, res['Reason'])
                self.assertEqual(self.head(self.board), before)
                # Reset the world for the next path: the board catches up out of band.
                self.git(self.board, 'pull', '-q', '--ff-only', 'origin', 'master')

    def test_board_owned_path_with_space_and_non_ascii_is_reported_verbatim(self):
        # DG-GIT-PATHLIST: a plain diff --name-only quotes this path as octal escapes, which
        # would then reach the refusal reason.
        rel = 'tools/hooks/h é x.ps1'
        self.commit(self.upstream, rel, 'changed\n', 'touch spaced non-ascii hook')
        before = self.head(self.board)
        res = self.run_ff()
        self.assert_refused_unless_mover(res)
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('hook-change-pending'), res['Reason'])
        self.assertIn('1 file(s)', res['Reason'])
        self.assertIn(rel, res['Reason'])
        self.assertNotIn('\\', res['Reason'])
        self.assertEqual(self.head(self.board), before)

    def test_dirty_tracked_path_with_space_and_non_ascii_counts_once(self):
        rel = 'docs/n é x.md'
        self.commit(self.upstream, rel, 'one\n', 'add spaced non-ascii file')
        self.git(self.board, 'pull', '-q', '--ff-only', 'origin', 'master')
        self.commit(self.upstream, 'src/a.cpp', 'x\n', 'second')
        (self.board / rel).write_text('local edit\n', encoding='utf-8')
        before = self.head(self.board)
        res = self.run_ff()
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('dirty-tracked (1 path(s))'), res['Reason'])
        self.assertEqual(self.head(self.board), before)

    def test_interval_skips_without_force(self):
        first = self.run_ff()
        self.assertEqual(first['Result'], 'current')
        self.assertTrue(self.state.exists())
        line = self.invoke_ff(self.ff_params(force=False), "if ($null -eq $r) { 'null' } else { 'object' }")
        self.assertEqual(line, 'null')

    def test_fetch_failure_is_refused(self):
        self.git(self.board, 'remote', 'set-url', 'origin', str(self.tmp / 'does-not-exist'))
        res = self.run_ff()
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('fetch-failed'), res['Reason'])


class BoardProfileTests(TempWorld):
    def load(self, path):
        cmd = ("Import-Module $env:BP_MODULE -Force; "
               "try { $p = Get-BoardProfile -Path $env:BP_PATH; 'OK ' + $p.project } "
               "catch { 'ERR ' + $_.Exception.GetType().Name + ' | ' + $_.Exception.Message }")
        env = dict(self.env, BP_MODULE=str(PROFILE_MODULE), BP_PATH=str(path))
        r = subprocess.run(PWSH + ['-Command', cmd], text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def write_profile(self, name, **overrides):
        data = json.loads(PROFILE.read_text(encoding='utf-8'))
        for k, v in overrides.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        p = self.tmp / name
        p.write_text(json.dumps(data), encoding='utf-8')
        return p

    def test_shipped_profile_loads(self):
        self.assertEqual(self.load(PROFILE), 'OK mlv-app')

    def test_every_required_key_is_enforced(self):
        keys = ['schema', 'project', 'boardRoot', 'remote', 'branch', 'productPaths', 'roadmap',
                'doctrineOutboxTarget', 'venues', 'boardOwnedPaths']
        shipped = json.loads(PROFILE.read_text(encoding='utf-8'))
        self.assertEqual(sorted(shipped), sorted(keys), 'profile keys drifted from the documented schema')
        for key in keys:
            with self.subTest(missing=key):
                out = self.load(self.write_profile(f'p-{key}.json', **{key: None}))
                self.assertEqual(out, f'ERR BoardProfileMissingKeyException | board-profile-missing-key: {key}')

    def test_empty_value_counts_as_missing(self):
        out = self.load(self.write_profile('p-empty.json', venues=[]))
        self.assertIn('board-profile-missing-key: venues', out)

    def test_unreadable_profile_is_typed(self):
        bad = self.tmp / 'bad.json'
        bad.write_text('{not json', encoding='utf-8')
        self.assertTrue(self.load(bad).startswith('ERR BoardProfileUnreadableException'))
        self.assertTrue(self.load(self.tmp / 'absent.json').startswith('ERR BoardProfileUnreadableException'))

    def test_script_defaults_come_from_the_profile(self):
        prof = self.write_profile('mine.json', boardRoot=str(self.board), remote='origin', branch='master')
        res = self.run_ff(profile=prof, explicit=False)
        self.assertEqual(res['Result'], 'current')

    def test_explicit_parameters_override_the_profile(self):
        prof = self.write_profile('wrong.json', boardRoot=str(self.tmp / 'nope'), remote='nowhere', branch='nobranch')
        res = self.run_ff(profile=prof, explicit=True)
        self.assertEqual(res['Result'], 'current')

    def test_profile_branch_drives_the_wrong_branch_refusal(self):
        prof = self.write_profile('b.json', boardRoot=str(self.board), remote='origin', branch='release')
        self.git(self.upstream, 'branch', 'release')
        res = self.run_ff(profile=prof, explicit=False)
        self.assertEqual(res['Result'], 'refused')
        self.assertIn("need release", res['Reason'])

    def test_profile_board_owned_paths_drive_the_refusal(self):
        prof = self.write_profile('o.json', boardRoot=str(self.board), remote='origin', branch='master',
                                  boardOwnedPaths=['gates/'])
        self.commit(self.upstream, 'gates/g.txt', 'x\n', 'gate change')
        res = self.run_ff(profile=prof, explicit=False)
        self.assertEqual(res['Result'], 'refused')
        self.assertTrue(res['Reason'].startswith('hook-change-pending'), res['Reason'])

    def test_state_and_scratch_default_outside_the_repo(self):
        env_scratch = self.tmp / 'envscratch'
        self.env['BOARD_FF_SCRATCH'] = str(env_scratch)
        res = self.run_ff(scratch=False)
        self.assertEqual(res['Result'], 'current')
        self.assertTrue((env_scratch / 'board-ff-state.json').exists())
        self.assertTrue((env_scratch / 'board-ff-fetch-last.txt').exists())
        status = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--', 'tools/coordination/autopilot'],
                                text=True, capture_output=True).stdout
        self.assertNotIn('board-ff-state', status)
        self.assertNotIn('board-ff-fetch', status)


class AsciiOnlyTests(unittest.TestCase):
    def test_scripts_are_ascii(self):
        for p in (BOARD_FF, PROFILE_MODULE):
            with self.subTest(file=p.name):
                p.read_bytes().decode('ascii')


if __name__ == '__main__':
    unittest.main()
